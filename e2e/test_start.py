"""Starting up when something is in the way: a server that cannot start, a join before the server runs."""
import os
import pty
import socket
import subprocess
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


def at_a_terminal(machine, args, answer):
    """Run a crewchat command as a person would in a terminal, typing `answer` when it asks."""
    leader, follower = pty.openpty()
    try:
        proc = subprocess.Popen(H.crewchat_command() + args, cwd=str(machine.folder), env=machine.env,
                                stdin=follower, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        os.write(leader, (answer + "\n").encode())
        out, _ = proc.communicate(timeout=60)
        return out.decode()
    finally:
        os.close(leader)
        os.close(follower)


class Unlinking(unittest.TestCase):
    def test_leaving_at_a_terminal_asks_first_and_names_the_machine(self):
        a, b = H.Machine("desk4"), H.Machine("laptop4")
        try:
            a.start()
            b.start()
            b.link_to(a)
            H.eventually(lambda: b.name in a.peers_status(), what="the link")
            out = at_a_terminal(b, ["peers", "leave"], "n")
            self.assertIn("This takes THIS machine, %s, out of the chat it shares with %s" % (b.name, a.name), out)
            self.assertIn("Nothing changed.", out)
            self.assertTrue((b.home / "peers.json").exists())
            out = at_a_terminal(a, ["peers", "remove", b.name], "")  # Enter alone means no
            self.assertIn("Nothing changed.", out)
            self.assertIn(b.name, a.peers_status())
            out = at_a_terminal(b, ["peers", "leave"], "y")
            self.assertIn("This machine left", out)
            self.assertFalse((b.home / "peers.json").exists())
            H.eventually(lambda: b.name not in a.peers_status(), timeout=30, what="the desk to forget it")
        finally:
            a.stop()
            b.stop()


if __name__ == "__main__":
    unittest.main()
