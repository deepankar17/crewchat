"""Stopping and restarting, and telling the owner a newer crewchat is out."""
import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer

import harness as H
from harness import eventually


class FakeGitHub:
    """GitHub's "latest release" answer, served locally."""

    def __init__(self, tag):
        tag_box = {"tag": tag}

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                body = json.dumps({"tag_name": tag_box["tag"]}).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args):
                pass

        self.tag = tag_box
        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = "http://127.0.0.1:%d/repos/deepankar17/crewchat/releases/latest" % self.server.server_address[1]

    def close(self):
        self.server.shutdown()


class StopRestart(unittest.TestCase):
    def test_stop_and_restart(self):
        m = H.Machine("stopper")
        try:
            m.start()
            a = m.agent()
            a.link()
            pid = m.server_pids()
            out = m.cli("stop")
            self.assertIn("Stopped crewchat", out)
            self.assertFalse(m.up())
            self.assertFalse(m.server_pids())
            self.assertIn("was not running", m.cli("stop"))
            self.assertIn("is running at %s" % m.url, m.cli("restart"))
            self.assertTrue(m.up())
            self.assertNotEqual(m.server_pids(), pid)
            # The agent's session carries on under its name.
            m._owner = None
            a.stop_hook()
            m.owner.send(a.name, "back after a stop")
            self.assertIn("back after a stop", a.stop_hook())
            self.assertIn("is running at", m.cli("restart"))  # restart while running
            self.assertTrue(m.up())
        finally:
            m.stop()


class NothingLostInACrash(unittest.TestCase):
    """state.json waits up to a second for read positions and presence, never for claims (roadmap P5)."""

    def test_claims_survive_kill_9_and_read_positions_a_stop(self):
        m = H.Machine("crasher")
        try:
            m.start()
            agents = [m.agent() for _ in range(3)]
            for a in agents:
                a.link()
            tids = [m.owner.task("all", "crash test %d" % i) for i in range(6)]
            for i, tid in enumerate(tids):
                agents[i % 3].call("hub_task", action="take", id=tid)
            m.kill(hard=True)  # kill -9, at once: nothing flushed
            m.cli("start", "--no-service", "--no-open", cwd=m.folder)
            m.wait_up()
            sheet = agents[0].call("hub_task", action="view", show="all")
            for i, tid in enumerate(tids):
                self.assertIn("#%s [doing] crash test %d (from Owner; held by %s" % (tid, i, agents[i % 3].name), sheet)
            # A stop (SIGTERM, crewchat stop) writes what waits: a message read just before is not shown again.
            for stop in (lambda: m.kill(hard=False), lambda: m.cli("stop")):
                m.owner.send(agents[1].name, "read me once")
                self.assertIn("read me once", agents[1].call("hub_inbox"))
                stop()
                m.cli("start", "--no-service", "--no-open", cwd=m.folder)
                m.wait_up()
                self.assertEqual(agents[1].call("hub_inbox"), "No new messages.")
        finally:
            m.stop()


class UpdateNotice(unittest.TestCase):
    def machine(self, github):
        m = H.Machine("notice")
        del m.env["CREWCHAT_NO_UPDATE_CHECK"]
        m.env["CREWCHAT_UPDATE_URL"] = github.url
        return m

    def test_a_newer_release_is_logged_and_shown_on_the_page(self):
        github = FakeGitHub("v99.0.0")
        m = self.machine(github)
        try:
            m.start()
            eventually(lambda: "crewchat 99.0.0 is available" in m.log(), what="the log line")
            self.assertIn("run `crewchat update`", m.log())
            self.assertEqual(m.owner.poll()["update"], "99.0.0")
            self.assertIn("Update:  crewchat 99.0.0 is available", m.cli("status"))
            self.assertIn("crewchat 99.0.0 is available", m.cli("start", "--no-service", "--no-open"))
        finally:
            m.stop()
            github.close()

    def test_no_notice_when_up_to_date_or_offline(self):
        github = FakeGitHub("v" + H.subprocess.run([H.sys.executable, str(H.ROOT / "crewchat.py"), "--version"],
                                                   capture_output=True, text=True).stdout.split()[1])
        m = self.machine(github)
        try:
            m.start()
            eventually(lambda: (m.home / "update.json").exists(), what="the check")
            self.assertEqual(m.owner.poll()["update"], "")
            self.assertNotIn("is available", m.log())
            self.assertNotIn("Update:", m.cli("status"))
        finally:
            m.stop()
            github.close()
        offline = H.Machine("offline")
        del offline.env["CREWCHAT_NO_UPDATE_CHECK"]
        offline.env["CREWCHAT_UPDATE_URL"] = "http://127.0.0.1:9/nothing-here"
        try:
            offline.start()
            self.assertEqual(offline.owner.poll()["update"], "")
            self.assertTrue(offline.up())
        finally:
            offline.stop()


if __name__ == "__main__":
    unittest.main()
