"""O1: an agent acts as the owner only with the owner's leave (owner, 5 and 14 Nov 2026). An owner command run inside an
AI agent's session asks the owner on a card in Needs you and runs only on Allow; agents' before-hooks refuse a call that
reads crewchat's token files. A soft block, as the owner chose: SECURITY.md says how far it goes."""
import argparse
import contextlib
import io
import json
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from crewchat_fixture import ROOT, TMP, Base, crewchat  # noqa: E402  (the shared server fixture)


def needs_item(hub, tool=None, wait=10):
    """The permission card for tool (OWNER_ACT_TOOL by default) once it is in Needs you."""
    tool = tool or crewchat.OWNER_ACT_TOOL
    end = time.time() + wait
    while time.time() < end:
        item = next((n for n in hub.needs_you() if n["kind"] == "permission" and n["tool"] == tool), None)
        if item:
            return item
        time.sleep(0.05)
    raise AssertionError("the card never reached Needs you")


class OwnerCommandsInAnAgentSession(unittest.TestCase):
    """The command line: owner_token asks for leave inside an agent's session, once a run, and stops without it."""

    def setUp(self):
        crewchat.OWNER_LEAVE.clear()
        self.addCleanup(crewchat.OWNER_LEAVE.clear)
        self.asked = []
        self.answer = {"answer": "allow", "note": ""}
        patches = [mock.patch.object(crewchat, "read_token", return_value="owner-secret"),
                   mock.patch.object(crewchat, "post_json", side_effect=self.post),
                   mock.patch.object(crewchat.sys, "argv", ["crewchat", "say", "all", "ship it"])]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def post(self, url, token, body, timeout=60, headers=None, **kw):
        if url.endswith("/api/owner-ask"):
            self.asked.append((token, body, timeout))
            if isinstance(self.answer, Exception):
                raise self.answer
            return self.answer
        return {"ok": True, "url": url}

    def env(self, **extra):
        clean = {k: v for k, v in os.environ.items() if k not in crewchat.AGENT_SESSION_ENV}
        return mock.patch.dict(crewchat.os.environ, dict(clean, **extra), clear=True)

    def test_the_card_shows_a_server_read_from_a_file(self):
        """Re-review 14 Nov: `crewchat mcp add --json FILE` showed the owner only the file's name on the card."""
        folder = Path(tempfile.mkdtemp(prefix="owner-leave-"))
        self.addCleanup(shutil.rmtree, folder, True)
        server = folder / "server.json"  # a file, not stdin: another test may have left sys.stdin closed
        server.write_text('{"name": "my-server", "command": "npx", "args": ["my-server-mcp"]}', encoding="utf-8")
        with self.env(CLAUDECODE="1"), mock.patch.object(crewchat.sys, "argv", ["crewchat", "mcp", "add", "--json", str(server)]), \
                mock.patch.object(crewchat, "local_url", return_value="http://127.0.0.1:8765"), \
                mock.patch.object(crewchat, "owner_call", side_effect=lambda *a, **k: crewchat.owner_token() and {
                    "added": [], "servers": []}), mock.patch("sys.stdout"):
            crewchat.cmd_mcp(argparse.Namespace(action="add", json=str(server), tools=None, allow=False, oauth=False))
        self.assertIn('"command": "npx"', self.asked[0][1]["command"])

    def test_uninstall_inside_an_agent_session_asks_even_with_yes(self):
        """Path-to-10 review (#178's "left for later"): `crewchat uninstall --yes` run by an agent stopped the server,
        took crewchat out of every folder and removed the program without the owner's leave."""
        self.answer = {"answer": "deny", "note": ""}
        stopped = []
        with self.env(CLAUDECODE="1"), mock.patch.object(crewchat.sys, "argv", ["crewchat", "uninstall", "--yes"]), \
                mock.patch.object(crewchat, "local_url", return_value="http://127.0.0.1:8765"), \
                mock.patch.object(crewchat, "stop_server", side_effect=lambda c: stopped.append(c)), \
                contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
            crewchat.cmd_uninstall(argparse.Namespace(yes=True))
        self.assertEqual(len(self.asked), 1)
        self.assertIn("crewchat uninstall --yes", self.asked[0][1]["command"])
        self.assertEqual(stopped, [])
        with self.env(CLAUDECODE="1"), mock.patch.object(crewchat, "read_token", return_value=""), \
                mock.patch.object(crewchat, "stop_server", side_effect=lambda c: stopped.append(c)), \
                self.assertRaises(SystemExit) as e:
            crewchat.cmd_uninstall(argparse.Namespace(yes=True))
        self.assertIn("own terminal", str(e.exception.code))
        self.assertEqual(stopped, [])

    def test_a_command_too_long_for_the_card_is_not_asked(self):
        crewchat.OWNER_LEAVE["detail"] = "x" * crewchat.MAX_PERMISSION_TEXT
        with self.env(CLAUDECODE="1"), self.assertRaises(SystemExit):
            crewchat.owner_token()
        self.assertEqual(self.asked, [])

    def test_the_owner_in_a_terminal_is_not_asked(self):
        with self.env():
            self.assertEqual(crewchat.owner_token(), "owner-secret")
            self.assertEqual(crewchat.owner_call("/api/send", {"to": "all", "text": "hi"})["url"][-9:], "/api/send")
        self.assertEqual(self.asked, [])

    def test_every_agent_tool_s_variable_counts(self):
        for name in ("CLAUDECODE", "CURSOR_AGENT", "GEMINI_CLI", "QWEN_CODE", "CODEX_SANDBOX", "OPENCODE",
                     "CREWCHAT_LINK_KEY"):
            with self.env(**{name: "1"}):
                self.assertEqual(crewchat.agent_session(), name)
        with self.env(CLAUDECODE=""):
            self.assertEqual(crewchat.agent_session(), "")

    def test_an_agent_session_asks_once_with_the_whole_command_and_runs_on_allow(self):
        with self.env(CLAUDECODE="1", CREWCHAT_LINK_KEY="key-of-leo"), contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(crewchat.owner_call("/api/send", {"to": "all", "text": "ship it"})["url"][-9:], "/api/send")
            crewchat.owner_token()  # a second call in the same run: the owner answered already
        (token, body, timeout), = self.asked
        self.assertEqual(token, "owner-secret")
        self.assertEqual(body, {"command": "crewchat say all 'ship it'", "from": "CREWCHAT_LINK_KEY", "key": "key-of-leo"})
        self.assertGreater(timeout, crewchat.PERMISSION_WAIT)  # it waits as long as the card does
        self.assertIn("owner's leave", err.getvalue())

    def test_a_deny_no_answer_or_no_server_stops_the_command(self):
        for answer, said in (({"answer": "deny", "note": "Ask me first"}, "did not allow it (Ask me first)"),
                             ({"answer": "", "note": ""}, "did not allow it"),
                             (urllib.error.URLError("refused"), "not running here"),
                             (urllib.error.HTTPError("u", 403, "x", {}, io.BytesIO(b'{"error": "only the owner"}')),
                              "only the owner")):
            self.answer = answer
            crewchat.OWNER_LEAVE.clear()
            with self.env(CLAUDECODE="1"), contextlib.redirect_stderr(io.StringIO()), \
                    self.assertRaises(SystemExit) as stop:
                crewchat.owner_call("/api/send", {"to": "all", "text": "x"})
            self.assertIn(said, str(stop.exception.code))
            self.assertIn("Ask the owner in the chat", str(stop.exception.code))
        self.assertFalse(crewchat.OWNER_LEAVE)

    def test_an_agent_s_own_crew_command_acts_as_that_agent_and_is_not_asked(self):
        with self.env(CREWCHAT_LINK_KEY="key-of-leo", CLAUDECODE="1"):
            crewchat.owner_call("/api/launch", {"agent_key": "key-of-leo", "name": "Quill"})
        self.assertEqual(self.asked, [])

    def test_a_command_the_page_runs_is_the_owner_s_even_from_a_server_an_agent_started(self):
        seen = {}

        def run(argv, **kw):
            seen.update(kw["env"])
            return mock.Mock(returncode=0, stdout="", stderr="")
        with self.env(CLAUDECODE="1", CREWCHAT_LINK_KEY="key-of-leo"), mock.patch.object(crewchat.subprocess, "run", run):
            crewchat.run_crewchat(["status"])
        self.assertFalse(set(crewchat.AGENT_SESSION_ENV) & set(seen))
        self.assertEqual(seen["CREWCHAT_FROM_SERVER"], "1")


class TheOwnersCard(unittest.TestCase):
    """The hub: the card in Needs you, Allow and Deny only, answered by nothing but the owner."""

    def setUp(self):
        root = Path(tempfile.mkdtemp(prefix="owner-leave-", dir=TMP))
        crewchat.save_config({"project": "web", "port": 9}, root)
        self.hub = crewchat.Hub(crewchat.Roster(root))
        self.addCleanup(self.hub.events.close)

    def ask(self, **kw):
        got = {}
        t = threading.Thread(target=lambda: got.update(self.hub.ask_owner_act(**kw)))
        t.start()
        return t, got

    def test_allow_lets_it_run_and_the_card_names_the_agent_and_the_command(self):
        name = self.hub.agent_for(self.hub.open_session("mac", "claude"))
        with self.hub.lock:
            self.hub.links["key-of-leo-1"] = name
        t, got = self.ask(command="crewchat say all 'ship it'", marker="CLAUDECODE", key="key-of-leo-1")
        item = needs_item(self.hub)
        self.assertEqual((item["agent"], item["choices"], item["rule"]), (name, ["allow", "deny"], False))
        self.assertIn("crewchat say all 'ship it'", item["text"])
        self.assertIn("runs as you", item["text"])
        self.assertIn("CLAUDECODE", item["text"])
        with self.assertRaises(crewchat.HubError):
            self.hub.answer_permission(item["id"], "allow_always")  # never for good
        self.hub.answer_permission(item["id"], "allow")
        t.join(10)
        self.assertEqual(got, {"answer": "allow", "note": ""})

    def test_deny_carries_the_note_and_an_unknown_session_is_still_asked(self):
        t, got = self.ask(command="crewchat task add x", marker="GEMINI_CLI")
        item = needs_item(self.hub)
        self.assertEqual(item["agent"], "An agent session")
        self.hub.answer_permission(item["id"], "deny", "not now")
        t.join(10)
        self.assertEqual(got, {"answer": "deny", "note": "not now"})

    def test_a_linked_machine_cannot_answer_it(self):
        """Re-review 14 Nov: the card went to linked machines' pages like any permission prompt, so whoever held the
        link key (an agent that read it, too) could allow it from there. This machine's page or the phone only."""
        t, got = self.ask(command="crewchat say all hi", marker="CLAUDECODE")
        item = needs_item(self.hub)
        self.assertNotIn(item["key"], [i["key"] for i in self.hub.shared_needs()])
        with self.assertRaises(crewchat.HubError):
            self.hub.peer_need({"key": item["key"], "answer": "allow"})
        self.hub.answer_permission(item["id"], "deny")
        t.join(10)
        self.assertEqual(got["answer"], "deny")

    def test_no_rule_or_setting_answers_it_for_the_owner(self):
        self.hub.set_approvals(claude="crewchat")  # an Always allow that would cover a Bash call to crewchat
        crewchat.change_config(lambda c: c.update(permissions="terminal"), self.hub.roster.root)
        self.assertEqual(self.hub.ask_owner_act("crewchat say all hi", "CLAUDECODE", wait=0.3)["answer"], "")
        t, got = self.ask(command="crewchat say all hi", marker="CLAUDECODE")
        needs_item(self.hub)  # still on the page: no terminal asks for it
        self.hub.answer_permission(needs_item(self.hub)["id"], "deny")
        t.join(10)
        with self.assertRaises(crewchat.HubError):
            self.hub.ask_owner_act("   ")


class OverHttp(Base):
    """/api/owner-ask takes the owner token only, and waits for the owner's answer."""

    def test_only_the_owner_token_asks_and_the_answer_comes_back(self):
        with self.assertRaises(urllib.error.HTTPError) as e:
            crewchat.post_json(self.base + "/api/owner-ask", self.token, {"command": "crewchat say all x"})
        self.assertIn(e.exception.code, (401, 403))
        got = {}
        t = threading.Thread(target=lambda: got.update(crewchat.post_json(
            self.base + "/api/owner-ask", self.owner, {"command": "crewchat say all x", "from": "CLAUDECODE"})))
        t.start()
        item = needs_item(self.hub)
        crewchat.post_json(self.base + "/api/needs", self.owner, {"key": item["key"], "answer": "allow"})
        t.join(10)
        self.assertEqual(got, {"answer": "allow", "note": ""})


class TokenFilesRefused(unittest.TestCase):
    """The before-hook on shell, edit and read calls refuses one that names crewchat's token files; other reads go on
    without asking anyone."""

    def run_hook(self, client, data):
        out = io.StringIO()
        with mock.patch.object(crewchat, "hook_post", side_effect=AssertionError("asked the server")), \
                contextlib.redirect_stdout(out):
            crewchat.write_hook(client, dict(data, session_id="none-" + client))
        return out.getvalue()

    def test_reading_a_token_file_is_refused_in_each_tool_s_words(self):
        home = str(crewchat.home())
        for client, data in (
                ("claude", {"tool_name": "Read", "tool_input": {"file_path": home + "/tokens/Owner.token"}}),
                ("claude", {"tool_name": "Bash", "tool_input": {"command": "cat ~/.crewchat/tokens/Owner.token"}}),
                ("claude", {"tool_name": "Bash", "tool_input": {"command": "ls ~/.crewchat//tokens"}}),
                ("claude", {"tool_name": "Bash", "tool_input": {"command": "cat ~/.crewchat/*/*"}}),
                ("claude", {"tool_name": "Bash", "tool_input": {"command": "cat ~/.crewchat/t?kens/x"}}),
                ("claude", {"tool_name": "Glob", "tool_input": {"pattern": "~/.crewchat/tokens/*"}}),
                ("claude", {"tool_name": "Write", "tool_input": {"file_path": home + "/tokens/Owner.token", "content": "x"}}),
                ("codex", {"tool_name": "apply_patch", "tool_input": {"input": "*** Begin Patch\n*** Update File: "
                                                                               "/u/.crewchat/tokens/Owner.token\n"}}),
                ("claude", {"tool_name": "Grep", "tool_input": {"pattern": "x", "path": home + "/tokens"}}),
                ("codex", {"tool_name": "shell", "tool_input": {"command": ["bash", "-lc", "cat Owner.token"]}}),
                ("gemini", {"tool_name": "read_file", "tool_input": {"absolute_path": "C:\\Users\\d\\.crewchat\\tokens\\Owner.token"}}),
                ("cursor", {"hook_event_name": "beforeReadFile", "file_path": home + "/tokens/place-mac.token"}),
                ("cursor", {"command": "type %USERPROFILE%\\.crewchat\\tokens\\Owner.token"})):
            out = self.run_hook(client, data)
            self.assertEqual(json.loads(out), json.loads(crewchat.browser_deny(client, crewchat.TOKEN_REFUSAL)), data)

    def test_the_secrets_beside_the_tokens_are_refused_too(self):
        """Re-review 14 Nov: peers.json holds the linked machines' shared key, with which an agent could answer Needs
        you (its own Allow card included) as another machine. The push key, cloud keys and sign-ins likewise."""
        home = str(crewchat.home())
        for data in ({"tool_name": "Bash", "tool_input": {"command": "cat ~/.crewchat/peers.json"}},
                     {"tool_name": "Read", "tool_input": {"file_path": home + "/peers.json"}},
                     {"tool_name": "Read", "tool_input": {"file_path": home + "/projects/web/peers.json"}},
                     {"tool_name": "Bash", "tool_input": {"command": "cat ~/.crewchat/push.json"}},
                     {"tool_name": "Bash", "tool_input": {"command": "cat ~/.crewchat/cloud/keys.json"}},
                     {"tool_name": "Read", "tool_input": {"file_path": home + "/config.json"}},
                     {"tool_name": "Grep", "tool_input": {"pattern": "secret", "path": home + "/cloud"}},
                     {"tool_name": "Read", "tool_input": {"file_path": home + "/folders.json"}},
                     # The folder itself (round-2 re-review): a search or an archive of it reads every file in it.
                     {"tool_name": "Bash", "tool_input": {"command": "grep -r token ~/.crewchat/"}},
                     {"tool_name": "Bash", "tool_input": {"command": "tar czf /tmp/x.tgz ~/.crewchat"}},
                     {"tool_name": "Bash", "tool_input": {"command": "cd ~/.crewchat && cat *.json"}},
                     {"tool_name": "Bash", "tool_input": {"command": "ls ~/.crewchat/"}},
                     {"tool_name": "Grep", "tool_input": {"pattern": "secret", "path": home}},
                     {"tool_name": "Grep", "tool_input": {"pattern": "secret", "path": home + "/"}},
                     # A project's home holds its own tokens (audit, round 3).
                     {"tool_name": "Bash", "tool_input": {"command": "cat ~/.crewchat/projects/web/tokens/place-y.token"}},
                     {"tool_name": "Bash", "tool_input": {"command": "cat ~/.crewchat/projects/*/tokens/*.token"}},
                     {"tool_name": "Bash", "tool_input": {"command": "grep -r s ~/.crewchat/projects"}},
                     {"tool_name": "Bash", "tool_input": {"command": "tar c ~/.crewchat/projects/web"}},
                     {"tool_name": "Bash", "tool_input": {"command": "ls ~/.crewchat/."}},
                     # The agents' link keys (round-3 re-review).
                     {"tool_name": "Bash", "tool_input": {"command": "cat ~/.crewchat/state.json"}},
                     {"tool_name": "Read", "tool_input": {"file_path": home + "/projects/web/state.json"}},
                     {"tool_name": "Grep", "tool_input": {"pattern": "secret", "path": home + "/projects/web"}}):
            out = self.run_hook("claude", data)
            self.assertEqual(json.loads(out), json.loads(crewchat.browser_deny("claude", crewchat.TOKEN_REFUSAL)), data)
        for data in ({"tool_name": "Read", "tool_input": {"file_path": "/repo/config.json"}},
                     {"tool_name": "Read", "tool_input": {"file_path": "/repo/peers.json"}},
                     {"tool_name": "Read", "tool_input": {"file_path": home + "/hub.log"}},
                     {"tool_name": "Bash", "tool_input": {"command": "tail ~/.crewchat/projects/web/hub.log"}}):
            self.assertEqual(self.run_hook("claude", data), "", data)

    def test_a_workspace_folder_s_secrets_are_refused_too(self):
        """#178 re-review: a project kept in a workspace folder outside the crewchat home has its tokens and settings
        (the Google Chat sign-in in config.json) at that folder's top; the list of workspaces and the held ones are
        the owner's too. A code folder inside the workspace is not."""
        listed = Path(crewchat.home()) / crewchat.WORKSPACES_FILE
        ws = Path(tempfile.mkdtemp(prefix="Work Space-", dir=TMP)) / "Bluebird"
        win = "C:\\Users\\Rahul Kumar\\crewchat-workspaces\\Hindi"
        user_ws = str(Path.home() / "crewchat-workspaces" / "Pay")
        had = listed.read_bytes() if listed.exists() else None
        listed.write_text(json.dumps({"format": 1, "workspaces": [
            {"id": "a", "name": "Bluebird", "path": str(ws)}, {"id": "b", "name": "Hindi", "path": win},
            {"id": "c", "name": "Pay", "path": user_ws}]}))
        try:
            w = str(ws)
            for data in ({"tool_name": "Bash", "tool_input": {"command": "cat '%s/tokens/place-api.token'" % w}},
                         {"tool_name": "Read", "tool_input": {"file_path": w + "/config.json"}},
                         {"tool_name": "Read", "tool_input": {"file_path": w + "/peers.json"}},
                         {"tool_name": "Bash", "tool_input": {"command": "grep -r x '%s'" % w}},
                         {"tool_name": "Grep", "tool_input": {"pattern": "secret", "path": w + "/"}},
                         {"tool_name": "Bash", "tool_input": {"command": "cat '%s'/*.json" % w}},
                         {"tool_name": "Bash", "tool_input": {"command": "ls '%s/.'" % w}},
                         {"tool_name": "Read", "tool_input": {"file_path": win + "\\tokens\\Owner.token"}},
                         {"tool_name": "Bash", "tool_input": {"command": "cat ~/crewchat-workspaces/Pay/config.json"}},
                         {"tool_name": "Write", "tool_input": {"file_path": str(crewchat.home()) + "/held-workspaces.json",
                                                               "content": "{}"}},
                         {"tool_name": "Edit", "tool_input": {"file_path": str(crewchat.home()) + "/workspaces.json"}},
                         {"tool_name": "Bash", "tool_input": {"command": "echo {} > ~/.crewchat/held-workspaces.json"}},
                         # Round-7 review: the folder that holds the workspaces, searched or packed whole.
                         {"tool_name": "Bash", "tool_input": {"command": "grep -r secret ~/crewchat-workspaces"}},
                         {"tool_name": "Bash", "tool_input": {"command": "tar czf a.tgz ~/crewchat-workspaces/"}},
                         {"tool_name": "Grep", "tool_input": {"pattern": "x", "path": "C:\\Users\\Rahul Kumar\\crewchat-workspaces"}}):
                out = self.run_hook("claude", data)
                self.assertEqual(json.loads(out), json.loads(crewchat.browser_deny("claude", crewchat.TOKEN_REFUSAL)),
                                 data)
            for data in ({"tool_name": "Read", "tool_input": {"file_path": w + "/app/config.json"}},
                         {"tool_name": "Bash", "tool_input": {"command": "grep -r x '%s/app/src'" % w}},
                         {"tool_name": "Read", "tool_input": {"file_path": w + "-old/config.json"}},
                         {"tool_name": "Read", "tool_input": {"file_path": w + "/tokens.md"}},
                         # Audit: a code folder kept as projects/NAME in a workspace, and a cloud/ folder there.
                         {"tool_name": "Bash", "tool_input": {"command": "git -C '%s/projects/app' status" % w}},
                         {"tool_name": "Bash", "tool_input": {"command": "ls '%s/cloud/'" % w}},
                         # The user's home and a drive's top hold workspaces too, and everything else: not refused.
                         {"tool_name": "Bash", "tool_input": {"command": "grep -r x ~/notes"}},
                         {"tool_name": "Grep", "tool_input": {"pattern": "x", "path": str(Path.home())}},
                         {"tool_name": "Bash", "tool_input": {"command": "ls ~/crewchat-workspaces/notes"}},
                         # Another folder that holds a workspace holds the owner's code too: an agent works there.
                         {"tool_name": "Bash", "tool_input": {"command": "cd '%s' && git status" % ws.parent}},
                         {"tool_name": "Bash", "tool_input": {"command": "grep -r x '%s'" % ws.parent}}):
                self.assertEqual(self.run_hook("claude", data), "", data)
            script = Path(tempfile.mkdtemp(prefix="hook-script-", dir=TMP)) / "hook.py"
            script.write_text(crewchat.hook_script())
            import subprocess
            out = subprocess.run([sys.executable, str(script), "claude", "write"], capture_output=True, text=True,
                                 env=dict(os.environ, CREWCHAT_HOME=str(crewchat.home())),
                                 input=json.dumps({"session_id": "x", "tool_name": "Read",
                                                   "tool_input": {"file_path": w + "/tokens/Owner.token"}}))
            self.assertEqual(json.loads(out.stdout)["hookSpecificOutput"]["permissionDecision"], "deny", out.stderr)
            out = subprocess.run([sys.executable, str(script), "claude", "write"], capture_output=True, text=True,
                                 env=dict(os.environ, CREWCHAT_HOME=str(crewchat.home())),
                                 input=json.dumps({"session_id": "x", "tool_name": "Bash",
                                                   "tool_input": {"command": "grep -r x ~/crewchat-workspaces"}}))
            self.assertEqual(json.loads(out.stdout)["hookSpecificOutput"]["permissionDecision"], "deny", out.stderr)
        finally:
            if had is None:
                listed.unlink()
            else:
                listed.write_bytes(had)

    def test_a_damaged_list_of_workspaces_refuses_nothing_more(self):
        listed = Path(crewchat.home()) / crewchat.WORKSPACES_FILE
        had = listed.read_bytes() if listed.exists() else None
        try:
            for text in ("{", "[]", '{"workspaces": 3}', '{"workspaces": [{"path": ""}, 5, {"path": 7}]}'):
                listed.write_text(text)
                self.assertEqual(crewchat.workspace_roots(), [], text)
                self.assertEqual(self.run_hook("claude", {"tool_name": "Read", "tool_input": {
                    "file_path": "/repo/config.json"}}), "", text)
        finally:
            if had is None:
                listed.unlink()
            else:
                listed.write_bytes(had)

    def test_other_reads_go_on_without_asking(self):
        for client, data in (("claude", {"tool_name": "Read", "tool_input": {"file_path": "/repo/src/tokens.py"}}),
                             ("claude", {"tool_name": "Grep", "tool_input": {"pattern": "design tokens"}}),
                             ("claude", {"tool_name": "Read", "tool_input": {"file_path": str(crewchat.home()) + "/tokens-old.md"}}),
                             ("cursor", {"hook_event_name": "beforeReadFile", "file_path": "/repo/README.md",
                                         "content": "keep ~/.crewchat/tokens/Owner.token private"}),
                             ("claude", {"tool_name": "Grep", "tool_input": {"pattern": "Owner.token", "path": "/repo"}}),
                             ("claude", {"tool_name": "Bash", "tool_input": {"command": "ls ~/.crewchat/hub.log"}}),
                             ("claude", {"tool_name": "Bash", "tool_input": {"command": "cat ~/.crewchat-notes.md"}})):
            self.assertEqual(self.run_hook(client, data), "", data)

    def test_writing_about_the_token_files_is_not_reading_them(self):
        """What an edit writes is text: a doc (SECURITY.md, this change's own guide) may name the token files."""
        said = []
        post = lambda url, token, body, timeout=5: said.append(body) or {}  # noqa: E731
        for data in ({"tool_name": "Edit", "tool_input": {"file_path": "/repo/SECURITY.md", "old_string": "a",
                                                          "new_string": "read `~/.crewchat/tokens/Owner.token`"}},
                     {"tool_name": "Write", "tool_input": {"file_path": "/repo/notes.md", "content": "owner.token"}}):
            self.assertFalse(crewchat.reads_token_files(data), data)
        out = io.StringIO()
        with mock.patch.object(crewchat, "hook_post", post), contextlib.redirect_stdout(out):
            crewchat.write_hook("claude", {"session_id": "none-edit", "tool_name": "Edit", "tool_input": {
                "file_path": "/repo/SECURITY.md", "new_string": "~/.crewchat/tokens/"}})
        self.assertEqual(out.getvalue(), "")

    def test_every_tool_s_read_calls_reach_the_hook(self):
        import re
        for tool in crewchat.READ_TOOLS | {"Bash", "Write"}:
            self.assertTrue(re.search(crewchat.WRITE_MATCHER, tool), tool)
        for tool in ("TodoWrite", "WebFetch", "mcp__crewchat__hub_send", "Reader"):
            self.assertFalse(re.search(crewchat.WRITE_MATCHER, tool), tool)
        hooks = {}
        crewchat.set_cursor_hooks(hooks)
        self.assertEqual(hooks["hooks"]["beforeReadFile"][-1]["command"], crewchat.hook_command("cursor", "write"))
        self.assertIn("read|grep|glob", crewchat.OPENCODE_PLUGIN)

    def test_the_light_hook_script_refuses_too(self):
        script = Path(tempfile.mkdtemp(prefix="hook-script-", dir=TMP)) / "hook.py"
        script.write_text(crewchat.hook_script())
        import subprocess
        out = subprocess.run([sys.executable, str(script), "claude", "write"], capture_output=True, text=True,
                             input=json.dumps({"session_id": "x", "tool_name": "Read",
                                               "tool_input": {"file_path": "/u/.crewchat/tokens/Owner.token"}}))
        self.assertEqual(json.loads(out.stdout)["hookSpecificOutput"]["permissionDecision"], "deny", out.stderr)


class TheGuideAndTheDocs(unittest.TestCase):

    def test_the_guide_and_security_md_say_what_is_enforced(self):
        self.assertIn("waits for the owner to allow it", crewchat.RULES)
        security = (ROOT / "SECURITY.md").read_text(encoding="utf-8")
        for name in crewchat.AGENT_SESSION_ENV:
            self.assertIn("`%s`" % name, security)
        self.assertIn("soft blocks", security)


class NewerKeysNotShared(unittest.TestCase):
    """Round-2 re-review: a copy of the linked machines' key, the phone push key or cloud sync's keys in a project
    could be shared to agents on other machines: the share check did not know them."""

    def test_they_count_as_stored_tokens(self):
        root = Path(tempfile.mkdtemp(prefix="newer-keys-"))
        self.addCleanup(shutil.rmtree, root, True)
        (root / "cloud").mkdir()
        (root / "projects" / "web").mkdir(parents=True)
        (root / "peers.json").write_text(json.dumps({"secret": "s" * 43, "members": {"a": {"url": "http://x:1"}}}))
        (root / "projects" / "web" / "peers.json").write_text(json.dumps({"secret": "p" * 43}))
        (root / "push.json").write_text(json.dumps({"vapid": "a" * 64, "subscriptions": [{"endpoint": "https://e"}]}))
        (root / "cloud" / "device.json").write_text(json.dumps({"private_key": "k" * 40, "name": "a" * 30}))
        (root / "cloud" / "device_key.pem").write_text("-----BEGIN PRIVATE KEY-----\n" + "m" * 64 + "\n-----END PRIVATE KEY-----\n")
        found = crewchat.stored_tokens([root])
        for key in ("s" * 43, "p" * 43, "a" * 64, "k" * 40, "m" * 64):
            self.assertIn(key.encode(), found)
        self.assertNotIn(("a" * 30).encode(), found)  # a name is not a key
        self.assertNotIn(b"http://x:1", found)


if __name__ == "__main__":
    unittest.main()
