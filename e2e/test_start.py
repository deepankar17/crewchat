"""Starting up when something is in the way: a server that cannot start, a join before the server runs."""
import socket
import unittest

import harness as H


class Start(unittest.TestCase):
    def test_a_server_that_cannot_start_says_why(self):
        m = H.Machine("busy")
        try:
            m.start()
            m.kill(hard=False)
            with socket.socket() as squatter:  # something else now holds the chat's port
                squatter.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # past TIME_WAIT
                squatter.bind(("127.0.0.1", m.port))
                squatter.listen(1)
                out = m.cli("start", "--no-service", "--no-open", check=False)
            self.assertIn("the server did not start", out)
            self.assertIn("cannot listen on 127.0.0.1:%d" % m.port, out)  # the log's own words
            m.cli("start", "--no-service", "--no-open")  # the port is free again
            self.assertTrue(m.up())
        finally:
            m.stop()

    def test_joining_before_the_server_runs_keeps_the_code(self):
        a, b = H.Machine("desk3"), H.Machine("laptop3")
        try:
            a.start()
            b.start()
            b.kill(hard=False)
            invite = a.cli("peers", "invite", "--url", a.url, "--name", a.name)
            line = next(l for l in invite.splitlines() if "crewchat peers join" in l).split()
            target, code = line[line.index("join") + 1], line[line.index("join") + 2]
            out = b.cli("peers", "join", target, code, "--url", b.url, "--name", b.name, check=False)
            self.assertIn("server is not running here", out)
            self.assertIn("the code still works", out)
            self.assertNotIn(b.name, a.peers_status())  # the desk did not count it in
            self.assertFalse((b.home / "peers.json").exists())
            b.cli("start", "--no-service", "--no-open")
            b.wait_up()
            b.cli("peers", "join", target, code, "--url", b.url, "--name", b.name)
            H.eventually(lambda: "online" in a.peers_status() and b.name in a.peers_status(), timeout=30,
                         what="the desk to see the laptop")
        finally:
            a.stop()
            b.stop()


if __name__ == "__main__":
    unittest.main()
