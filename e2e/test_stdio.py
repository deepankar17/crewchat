"""`crewchat stdio`: a client that only starts local servers (Claude Desktop, Glama's build) talks
MCP over stdin and stdout, relayed to the chat's server."""
import json
import queue
import subprocess
import threading
import time
import unittest

import harness as H


class Relay:
    """`crewchat stdio` running as a client would start it."""

    def __init__(self, machine, *args, cwd=None):
        self.proc = subprocess.Popen(H.crewchat_command() + ["stdio"] + list(args), cwd=str(cwd or machine.folder),
                                     env=machine.env, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE)
        self.lines = queue.Queue()
        threading.Thread(target=self._read, daemon=True).start()
        self.ids = 0

    def _read(self):
        for line in iter(self.proc.stdout.readline, b""):
            self.lines.put(json.loads(line))

    def write(self, message):
        self.proc.stdin.write((json.dumps(message) + "\n").encode())
        self.proc.stdin.flush()

    def request(self, method, params=None):
        self.ids += 1
        self.write({"jsonrpc": "2.0", "id": self.ids, "method": method, "params": params or {}})
        return self.ids

    def answer(self, rid=None, timeout=20):
        """The next answer (for request rid, if given; others seen first are kept)."""
        deadline = time.time() + timeout
        kept = []
        try:
            while True:
                msg = self.lines.get(timeout=max(0.1, deadline - time.time()))
                if rid is None or msg.get("id") == rid:
                    return msg
                kept.append(msg)
        except queue.Empty:
            raise AssertionError("no answer from crewchat stdio")
        finally:
            for msg in kept:
                self.lines.put(msg)

    def call(self, tool, **arguments):
        msg = self.answer(self.request("tools/call", {"name": tool, "arguments": arguments}))
        if "error" in msg:
            raise H.ToolError(msg["error"]["message"])
        return msg["result"]["content"][0]["text"]

    def hello(self, client="claude-ai"):
        msg = self.answer(self.request("initialize", {"protocolVersion": "2025-06-18", "capabilities": {},
                                                      "clientInfo": {"name": client, "version": "1"}}))
        self.write({"jsonrpc": "2.0", "method": "notifications/initialized"})
        return msg

    def close(self):
        self.proc.stdin.close()
        self.proc.wait(15)
        return self.proc.returncode

    def stop(self):
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(15)
        for pipe in (self.proc.stdin, self.proc.stdout, self.proc.stderr):
            pipe.close()


class Stdio(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = H.Machine("desk")
        cls.m.start()

    @classmethod
    def tearDownClass(cls):
        cls.m.stop()

    def test_a_desktop_client_joins_and_talks(self):
        relay = Relay(self.m, "--project", self.m.folder)
        try:
            hello = relay.hello()
            self.assertEqual(hello["result"]["serverInfo"]["name"], "crewchat")
            self.assertIn("hub_link", hello["result"]["instructions"])
            tools = relay.answer(relay.request("tools/list"))["result"]["tools"]
            self.assertIn("hub_send", [t["name"] for t in tools])
            me = next(l for l in relay.call("hub_agents").splitlines() if "(you)" in l).split(" (you)")[0]
            self.assertTrue(me.startswith("claude-desk"), me)
            relay.call("hub_send", to="Owner", text="hello from the desktop")
            self.m.owner.wait_for("hello from the desktop", **{"from": me})
            self.m.owner.send(me, "hello desktop")
            self.assertIn("hello desktop", relay.call("hub_inbox"))
            self.assertEqual(relay.close(), 0)
        finally:
            relay.stop()

    def test_a_waiting_inbox_does_not_hold_up_other_requests(self):
        relay = Relay(self.m, "--project", self.m.folder)
        try:
            relay.hello()
            relay.call("hub_inbox")
            waiting = relay.request("tools/call", {"name": "hub_inbox", "arguments": {"wait_seconds": 15}})
            start = time.time()
            self.assertIn("(you)", relay.call("hub_agents"))
            self.assertLess(time.time() - start, 5)
            name = next(l for l in relay.call("hub_agents").splitlines() if "(you)" in l).split(" (you)")[0]
            self.m.owner.send(name, "end the wait")
            self.assertIn("end the wait", relay.answer(waiting)["result"]["content"][0]["text"])
        finally:
            relay.stop()

    def test_from_a_sub_folder_and_with_an_address_and_token(self):
        sub = self.m.folder / "app" / "src"
        sub.mkdir(parents=True, exist_ok=True)
        relay = Relay(self.m, cwd=sub)  # finds the connected folder above it
        try:
            relay.hello()
            self.assertIn("(you)", relay.call("hub_agents"))
        finally:
            relay.stop()
        token = json.loads((self.m.folder / ".mcp.json").read_text())["mcpServers"]["crewchat"]["headers"]
        token = token["Authorization"][7:]
        relay = Relay(self.m, "--url", self.m.url, "--token", token, cwd=H.TMP)
        try:
            relay.hello()
            self.assertIn("(you)", relay.call("hub_agents"))
        finally:
            relay.stop()
        relay = Relay(self.m, "--url", self.m.url, "--token", "wrong", cwd=H.TMP)
        try:
            msg = relay.answer(relay.request("initialize", {}))
            self.assertIn("wrong token", msg["error"]["message"])
        finally:
            relay.stop()

    def test_a_folder_that_is_not_connected(self):
        out = subprocess.run(H.crewchat_command() + ["stdio"], cwd=str(H.TMP), env=self.m.env, input=b"",
                             capture_output=True, timeout=30)
        self.assertNotEqual(out.returncode, 0)
        self.assertIn(b"not connected to a crewchat", out.stderr)
        self.assertEqual(out.stdout, b"")  # nothing a client could mistake for MCP

    def test_bad_lines_and_notifications(self):
        relay = Relay(self.m, "--project", self.m.folder)
        try:
            relay.hello()
            relay.proc.stdin.write(b"this is not json\n\n")
            relay.proc.stdin.flush()
            self.assertEqual(relay.answer()["error"]["code"], -32700)
            relay.write({"jsonrpc": "2.0", "method": "notifications/cancelled", "params": {"requestId": 99}})
            relay.write([{"jsonrpc": "2.0", "id": 500, "method": "ping"}, {"jsonrpc": "2.0", "id": 501, "method": "ping"}])
            batch = relay.answer()
            self.assertEqual(sorted(m["id"] for m in batch), [500, 501])  # the notification got no answer
        finally:
            relay.stop()


class StdioRestarts(unittest.TestCase):
    def test_the_relay_carries_on_when_the_server_restarts_and_says_when_it_is_down(self):
        m = H.Machine("desk2")
        relay = None
        try:
            m.start()
            relay = Relay(m, "--project", m.folder)
            relay.hello()
            self.assertIn("(you)", relay.call("hub_agents"))
            m.restart()
            self.assertIn("(you)", relay.call("hub_agents"))  # a new session, opened by the relay
            m.kill(hard=True)
            with self.assertRaises(H.ToolError) as e:
                relay.call("hub_agents")
            self.assertIn("cannot reach the crewchat server", str(e.exception))
            self.assertIn("crewchat start", str(e.exception))
            m.cli("start", "--no-service", "--no-open")
            m.wait_up()
            self.assertIn("(you)", relay.call("hub_agents"))
            self.assertEqual(relay.close(), 0)
        finally:
            if relay:
                relay.stop()
            m.stop()


if __name__ == "__main__":
    unittest.main()
