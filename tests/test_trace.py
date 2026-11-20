"""The step trace (K1): each agent's tool calls, kept by the server in events.sqlite, shown or exported by
`crewchat trace` as JSON lines or OpenTelemetry (OTLP JSON). Nothing is sent anywhere by crewchat itself."""
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from crewchat_fixture import TMP, Base, cli, crewchat


def light_script():
    """The light hook script's own namespace, as written (hook_script): a name it forgot to copy fails here, where the
    script itself would hide it in its except."""
    copy = {"__name__": "light"}
    exec(compile(crewchat.hook_script(), crewchat.HOOK_SCRIPT_FILE, "exec"), copy)
    return copy


class StepFile(unittest.TestCase):
    def setUp(self):
        self.session = "trace-" + crewchat.secrets.token_hex(4)
        self.state = crewchat.hook_state_file("claude", self.session)
        self.state.parent.mkdir(parents=True, exist_ok=True)
        self.state.write_text(json.dumps({"key": "k" * 12, "alive": time.time(), "doing": "", "doing_at": time.time()}))
        self.addCleanup(lambda: [p.unlink() for p in self.state.parent.glob(self.state.name + "*")])

    def run_hook(self, script, post, tool="Read", **data):
        script["find_project"] = lambda *a, **k: Path(TMP)
        script["client_config"] = lambda *a, **k: ("http://127.0.0.1:9", "tok")
        script["hooks_off"] = lambda *a, **k: False
        script["hook_post"] = post
        call = dict({"session_id": self.session, "tool_name": tool, "tool_input": {"file_path": "/x/secret-name.py",
                                                                                     "content": "password=hunter2"}}, **data)
        script["tool_hook"]("claude", call)

    def test_the_light_hook_script_sends_each_tool_call_with_its_sign_of_life(self):
        script, sent = light_script(), []
        post = lambda url, token, body, timeout=5: sent.append(body) or {"steps": len(body.get("steps", []))}  # noqa: E731
        self.run_hook(script, post, "Read")  # within a minute of the last sign of life: kept, not sent
        self.run_hook(script, post, "Grep")
        self.assertEqual(sent, [])
        self.state.write_text(json.dumps(dict(json.loads(self.state.read_text()), alive=0)))
        self.run_hook(script, post, "Edit")
        self.assertEqual([s["tool"] for s in sent[0]["steps"]], ["Read", "Grep", "Edit"])
        self.assertEqual(sent[0]["steps"][0]["doing"], "Reading secret-name.py")
        self.assertNotIn("hunter2", json.dumps(sent))  # never a call's arguments
        self.assertFalse(crewchat.steps_file(self.state).exists())  # taken: gone

    def test_steps_wait_when_the_chat_cannot_be_reached_and_go_with_the_next_call(self):
        script, sent = light_script(), []

        def down(url, token, body, timeout=5):
            raise OSError("refused")
        self.state.write_text(json.dumps(dict(json.loads(self.state.read_text()), alive=0)))
        with self.assertRaises(OSError):
            self.run_hook(script, down, "Read")
        self.state.write_text(json.dumps(dict(json.loads(self.state.read_text()), alive=0)))
        self.run_hook(script, lambda url, token, body, timeout=5: sent.append(body) or {"steps": 2}, "Bash",
                      tool_input={"command": "npm test"})
        self.assertEqual([s["tool"] for s in sent[0]["steps"]], ["Read", "Bash"])

    def test_an_older_chat_that_keeps_no_trace_does_not_make_them_pile_up(self):
        script = light_script()
        self.state.write_text(json.dumps(dict(json.loads(self.state.read_text()), alive=0)))
        self.run_hook(script, lambda url, token, body, timeout=5: {"ok": True}, "Read")  # no "steps" in its answer
        self.assertFalse(crewchat.steps_file(self.state).exists())

    def test_a_full_file_is_sent_at_once(self):
        script, sent = light_script(), []
        crewchat.steps_file(self.state).write_bytes(b"x" * crewchat.STEP_FLUSH_BYTES + b"\n")
        self.run_hook(script, lambda url, token, body, timeout=5: sent.append(body) or {"steps": 1}, "Read")
        self.assertEqual(len(sent), 1)  # within the minute, but this many wait

    def test_a_full_file_waits_a_minute_after_a_failed_post(self):
        # A chat that cannot be reached must not cost every tool call a post (the hook never waits).
        script, calls = light_script(), []

        def down(url, token, body, timeout=5):
            calls.append(body)
            raise OSError("refused")
        crewchat.steps_file(self.state).write_bytes(b"x" * crewchat.STEP_FLUSH_BYTES + b"\n")
        with self.assertRaises(OSError):
            self.run_hook(script, down, "Read")
        self.run_hook(script, down, "Read")  # within the minute: kept, not posted again
        self.assertEqual(len(calls), 1)

    def test_other_tools_names_are_kept_as_claude_codes(self):
        crewchat.add_step(self.state, "gemini", {"tool_name": "run_shell_command", "tool_input": {"command": "ls"}})
        steps, taken = crewchat.take_steps(self.state)
        crewchat.sent_steps(self.state, taken, {"steps": 1})
        self.assertEqual(steps[0]["tool"], crewchat.OTHER_TOOL_NAMES["run_shell_command"])

    def test_the_stop_hook_sends_what_waits(self):
        # The tool hook sends at most once a minute: the turn's last steps go as it ends.
        crewchat.add_step(self.state, "claude", {"tool_name": "Read"})
        sent = []
        with mock.patch.object(crewchat, "hook_post", lambda url, token, body, timeout=5: sent.append(body) or {"steps": 1}):
            crewchat.report_steps("http://127.0.0.1:9", "tok", "k" * 12, self.state)
        self.assertEqual((sent[0]["event"], [s["tool"] for s in sent[0]["steps"]]), ("steps", ["Read"]))
        self.assertFalse(crewchat.steps_file(self.state).exists())

    def test_parallel_tool_calls_each_keep_their_step(self):
        # Claude Code runs a turn's independent calls at once: their hooks append, none overwrites another's.
        threads = [threading.Thread(target=crewchat.add_step, args=(self.state, "claude", {"tool_name": "Read"}))
                   for _ in range(40)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        steps, taken = crewchat.take_steps(self.state)
        self.assertEqual(len(steps), 40)
        crewchat.sent_steps(self.state, taken, {"steps": 40})

    def test_what_waits_has_a_limit(self):
        with mock.patch.object(crewchat, "STEP_FILE_MAX", 300):
            for _ in range(20):
                crewchat.add_step(self.state, "claude", {"tool_name": "Read"})
        self.assertLessEqual(crewchat.steps_file(self.state).stat().st_size, 300)


class TraceStore(unittest.TestCase):
    def setUp(self):
        root = Path(tempfile.mkdtemp(prefix="trace-", dir=TMP))
        crewchat.save_config({"project": "trace", "port": 9}, root)
        self.hub = crewchat.Hub(crewchat.Roster(root))
        self.addCleanup(self.hub.events.close)
        self.name = self.hub.agent_for(self.hub.open_session("mac", "claude"))
        self.key = crewchat.secrets.token_urlsafe(12)
        with self.hub.lock:
            self.hub.links[self.key] = self.name
        self.place = self.hub.agents[self.name]["place"]

    def test_the_server_keeps_a_linked_agents_steps(self):
        now = time.time()
        steps = [{"ts": now - 2, "tool": "Read", "doing": "Reading a.py"}, {"ts": now - 1, "tool": "Bash", "doing": "Running npm"},
                 {"ts": "not a time", "tool": "Grep"}, {"ts": now, "tool": ""}]
        self.assertEqual(self.hub.record_steps(self.place, self.key, steps), 2)
        self.assertEqual([(s["agent"], s["tool"], s["doing"]) for s in self.hub.events.steps()],
                         [(self.name, "Read", "Reading a.py"), (self.name, "Bash", "Running npm")])
        self.assertEqual(self.hub.record_steps("elsewhere", self.key, steps), 0)  # another place's key: not its agent
        self.assertEqual(self.hub.record_steps(self.place, "x" * 12, steps), 0)  # not linked

    def test_nothing_is_kept_when_this_machine_keeps_activity_off_the_page(self):
        with mock.patch.object(self.hub, "machine_config", return_value={"activity": "off"}):
            self.assertEqual(self.hub.record_steps(self.place, self.key, [{"ts": time.time(), "tool": "Read"}]), 0)
        self.assertEqual(self.hub.events.steps(), [])

    def test_a_renamed_agents_trace_is_the_new_names(self):
        self.hub.record_steps(self.place, self.key, [{"ts": time.time(), "tool": "Read"}])
        self.hub.events.rename(self.name, "nova")
        self.assertEqual([s["agent"] for s in self.hub.events.steps()], ["nova"])

    def test_old_steps_go(self):
        now = time.time()
        self.hub.events.add_steps("a", [(now - (crewchat.TRACE_DAYS + 1) * 86400, "Read", ""), (now - 60, "Edit", "")], now)
        self.assertEqual([s["tool"] for s in self.hub.events.steps()], ["Edit"])


class OtlpExport(unittest.TestCase):
    STEPS = [{"ts": 1000.0, "agent": "luca", "tool": "Read", "doing": "Reading a.py"},
             {"ts": 1003.5, "agent": "luca", "tool": "Edit", "doing": "Editing a.py"},
             {"ts": 1004.0, "agent": "jade", "tool": "Bash", "doing": ""},
             {"ts": 2000.0, "agent": "luca", "tool": "Bash", "doing": "Running pytest"}]

    def test_each_run_is_a_trace_with_a_span_for_each_tool_call(self):
        data = crewchat.otlp_trace(self.STEPS, "demo", "2.1.0")
        spans = data["resourceSpans"][0]["scopeSpans"][0]["spans"]
        roots = [s for s in spans if "parentSpanId" not in s]
        self.assertEqual([r["name"] for r in roots], ["luca run", "jade run", "luca run"])  # a long gap: a new run
        self.assertEqual(len({r["traceId"] for r in roots}), 3)
        first = [s for s in spans if s["traceId"] == roots[0]["traceId"] and s.get("parentSpanId")]
        self.assertEqual([s["name"] for s in first], ["Read", "Edit"])
        self.assertEqual(first[0]["parentSpanId"], roots[0]["spanId"])
        self.assertEqual((first[0]["startTimeUnixNano"], first[0]["endTimeUnixNano"]), ("1000000000000", "1003500000000"))
        attrs = {a["key"]: a["value"]["stringValue"] for a in first[0]["attributes"]}
        self.assertEqual(attrs["gen_ai.tool.name"], "Read")
        self.assertEqual(attrs["gen_ai.agent.name"], "luca")
        for s in spans:
            self.assertRegex(s["traceId"], r"^[0-9a-f]{32}$")
            self.assertRegex(s["spanId"], r"^[0-9a-f]{16}$")
        self.assertEqual(crewchat.otlp_trace(self.STEPS, "demo", "2.1.0"), data)  # the same steps, the same trace


class TraceOverHttp(Base):
    def test_hooks_send_steps_and_the_owner_exports_them(self):
        session, name = self.agent()
        key = crewchat.secrets.token_urlsafe(12)
        session.text("hub_link", key=key)
        now = time.time()
        out = crewchat.post_json(self.base + "/api/hook", self.token, {"key": key, "event": "tool", "doing": "Reading",
                                                                        "steps": [{"ts": now - 1, "tool": "Read", "doing": "Reading a.py"}]})
        self.assertEqual(out["steps"], 1)
        out = crewchat.post_json(self.base + "/api/hook", self.token, {"key": key, "event": "steps",
                                                                        "steps": [{"ts": now, "tool": "Edit"}]})
        self.assertEqual(out, {"steps": 1})
        status, _, _ = self.raw("/api/trace", json.dumps({}).encode(), {"Authorization": "Bearer " + self.token,
                                                                        "Content-Type": "application/json"})
        self.assertEqual(status, 403)  # the owner's only
        self.hub.events.flush()
        mine = [s for s in crewchat.post_json(self.base + "/api/trace", self.owner, {"agent": name})["steps"]]
        self.assertEqual([s["tool"] for s in mine], ["Read", "Edit"])
        folder = Path(tempfile.mkdtemp(prefix="trace-out-", dir=TMP))
        cli("trace", "--agent", name, "--format", "otlp", "-o", str(folder / "t.json"))
        spans = json.loads((folder / "t.json").read_text())["resourceSpans"][0]["scopeSpans"][0]["spans"]
        self.assertEqual([s["name"] for s in spans], ["%s run" % name, "Read", "Edit"])
        lines = cli("trace", "--agent", name, "--format", "jsonl").splitlines()
        self.assertEqual([json.loads(line)["tool"] for line in lines], ["Read", "Edit"])
        self.assertIn("Reading a.py", cli("trace", "--agent", name))


if __name__ == "__main__":
    unittest.main()
