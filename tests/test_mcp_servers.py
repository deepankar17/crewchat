"""The owner's own MCP servers (E1 to E3): checked, written for every tool in every connected folder, and asked."""
import http.server
import io
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

if "crewchat" not in sys.modules:  # run alone: a home of its own (test_crewchat sets one when it runs first)
    _TMP = tempfile.mkdtemp(prefix="crewchat-mcp-test-")
    os.environ["CREWCHAT_HOME"] = str(Path(_TMP) / "home")
    os.environ.pop("CODEX_HOME", None)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import crewchat  # noqa: E402

TMP = tempfile.mkdtemp(prefix="crewchat-mcp-")
crewchat.user_home = lambda: Path(TMP) / "user"  # never the real ~/.codex
crewchat.cursor_exe = lambda root=None: None  # never this machine's Cursor agent (tests that need one patch it)

# A server block pasted as a server's docs give it (Claude Code's .mcp.json shape).
PASTED = {"mcpServers": {"my-server": {"type": "http", "url": "http://localhost:8792/mcp",
                                       "headers": {"Authorization": "Bearer <YOUR_API_KEY>"}}}}
KEY = "Bearer sk-live-0123456789abcdef"


def folder_for(client):
    folder = Path(tempfile.mkdtemp(prefix="mcp-%s-" % client, dir=TMP)).resolve()
    crewchat.install_client(client, folder, "http://127.0.0.1:9", "tok")
    return folder


def remote(name="my-server", url="http://localhost:8792/mcp", headers=None, **kw):
    return crewchat.mcp_server(name, url=url, headers={"Authorization": KEY} if headers is None else headers, **kw)


class Checks(unittest.TestCase):
    def test_a_placeholder_is_refused(self):
        name, fields, _ = crewchat.mcp_parse_json(json.dumps(PASTED))[0]
        with self.assertRaisesRegex(crewchat.HubError, "replace <YOUR_API_KEY> in Authorization with your key"):
            crewchat.mcp_server(name, **fields)

    def test_the_pasted_block_is_one_remote_server_with_its_header(self):
        block = json.loads(json.dumps(PASTED))
        block["mcpServers"]["my-server"]["headers"]["Authorization"] = KEY
        block["mcpServers"]["my-server"]["timeout"] = 30
        (name, fields, dropped), = crewchat.mcp_parse_json(json.dumps(block))
        server, secrets = crewchat.mcp_server(name, **fields)
        self.assertEqual((server["kind"], server["transport"], server["url"], server["headers"], server["auth"]),
                         ("remote", "http", "http://localhost:8792/mcp", ["Authorization"], "key"))
        self.assertEqual(secrets["headers"], {"Authorization": KEY})
        self.assertEqual(dropped, ["timeout"])
        self.assertNotIn(KEY, json.dumps(server))  # the key is kept apart

    def test_other_shapes_are_read(self):
        (name, fields, _), = crewchat.mcp_parse_json(json.dumps(
            {"mcp": {"local-one": {"type": "local", "command": ["npx", "-y", "some-mcp"], "environment": {"A": "b"}}}}))
        server, secrets = crewchat.mcp_server(name, **fields)
        self.assertEqual((server["command"], server["args"], secrets["env"]), ("npx", ["-y", "some-mcp"], {"A": "b"}))
        (_, fields, _), = crewchat.mcp_parse_json(json.dumps({"x": {"httpUrl": "https://a.example/mcp"}}))
        self.assertEqual(fields["url"], "https://a.example/mcp")
        (_, fields, _), = crewchat.mcp_parse_json(json.dumps({"x": {"url": "https://a.example/sse"}}))
        self.assertEqual(fields["transport"], "sse")

    def test_plain_http_only_on_this_machine(self):
        for url in ("http://localhost:1/mcp", "http://127.0.0.1:1/mcp", "http://[::1]:1/mcp", "https://a.example/mcp"):
            self.assertEqual(remote(url=url)[0]["url"], url)
        for url in ("http://a.example/mcp", "ftp://a.example/", "https://user:pw@a.example/mcp", "nope"):
            with self.assertRaises(crewchat.HubError, msg=url):
                remote(url=url)

    def test_names(self):
        for name in ("crewchat", "browser", "mobile", "atlassian", "Atlassian", "a b", "", "x" * 65):
            with self.assertRaises(crewchat.HubError, msg=name):
                remote(name=name)
        with self.assertRaises(crewchat.HubError):
            crewchat.mcp_server("x", url="https://a.example/", command="npx")
        with self.assertRaises(crewchat.HubError):
            crewchat.mcp_server("x", command="npx", headers={"A": "b"})


class Writer(unittest.TestCase):
    def test_every_tool_gets_a_remote_server_with_its_header_and_loses_it(self):
        wanted = [remote()]
        for client, rel, key, url_key in (("claude", ".mcp.json", "mcpServers", "url"),
                                          ("cursor", ".cursor/mcp.json", "mcpServers", "url"),
                                          ("gemini", ".gemini/settings.json", "mcpServers", "httpUrl"),
                                          ("qwen", ".qwen/settings.json", "mcpServers", "httpUrl"),
                                          ("opencode", "opencode.json", "mcp", "url")):
            folder = folder_for(client)
            changed, left = crewchat.sync_mcp_servers(folder, wanted)
            self.assertIn(rel, changed, client)
            self.assertEqual(left, [])
            entry = json.loads((folder / rel).read_text())[key]["my-server"]
            self.assertEqual(entry[url_key], "http://localhost:8792/mcp", client)
            self.assertEqual(entry["headers"], {"Authorization": KEY}, client)
            self.assertIn(crewchat.SERVER_NAME, json.loads((folder / rel).read_text())[key])
            if os.name != "nt":  # Windows has no file modes: every file reads as 0o666
                self.assertFalse(os.stat(folder / rel).st_mode & 0o077, client)  # it holds a key: private
            self.assertEqual(crewchat.sync_mcp_servers(folder, wanted), ([], []), client)  # up to date
            if client == "claude":
                settings = json.loads((folder / ".claude/settings.local.json").read_text())
                self.assertIn("my-server", settings["enabledMcpjsonServers"])
                self.assertNotIn("mcp__my-server", json.dumps(settings))  # its tools ask unless the owner allows
                self.assertEqual(entry["type"], "http")
            crewchat.sync_mcp_servers(folder, [])
            self.assertNotIn("my-server", json.loads((folder / rel).read_text())[key], client)
            self.assertFalse((folder / crewchat.MCP_LEDGER).exists(), client)
            if client == "claude":
                settings = json.loads((folder / ".claude/settings.local.json").read_text())
                self.assertNotIn("my-server", settings.get("enabledMcpjsonServers", []))

    def test_a_local_server_in_each_tools_spelling(self):
        wanted = [crewchat.mcp_server("db", command="npx", args=["-y", "db-mcp"], env={"DB_TOKEN": "t" * 20})]
        folder = folder_for("opencode")
        crewchat.sync_mcp_servers(folder, wanted)
        self.assertEqual(json.loads((folder / "opencode.json").read_text())["mcp"]["db"],
                         {"type": "local", "command": ["npx", "-y", "db-mcp"], "environment": {"DB_TOKEN": "t" * 20},
                          "enabled": True})
        folder = folder_for("cursor")
        crewchat.sync_mcp_servers(folder, wanted)
        self.assertEqual(json.loads((folder / ".cursor/mcp.json").read_text())["mcpServers"]["db"],
                         {"command": "npx", "args": ["-y", "db-mcp"], "env": {"DB_TOKEN": "t" * 20}})

    def test_only_the_tools_ticked(self):
        folder = folder_for("cursor")
        crewchat.sync_mcp_servers(folder, [remote(tools=["claude"])])
        self.assertNotIn("my-server", (folder / ".cursor/mcp.json").read_text())

    def test_allowed_without_asking(self):
        folder = folder_for("claude")
        crewchat.sync_mcp_servers(folder, [remote(allow=True)])
        settings = json.loads((folder / ".claude/settings.local.json").read_text())
        self.assertIn("mcp__my-server", settings["permissions"]["allow"])
        crewchat.sync_mcp_servers(folder, [remote()])
        settings = json.loads((folder / ".claude/settings.local.json").read_text())
        self.assertNotIn("mcp__my-server", json.dumps(settings))
        folder = folder_for("gemini")
        crewchat.sync_mcp_servers(folder, [remote(allow=True)])
        self.assertTrue(json.loads((folder / ".gemini/settings.json").read_text())["mcpServers"]["my-server"]["trust"])

    def test_a_change_is_written(self):
        folder = folder_for("cursor")
        crewchat.sync_mcp_servers(folder, [remote()])
        crewchat.sync_mcp_servers(folder, [remote(headers={"Authorization": "Bearer new-key-0123456789"})])
        entry = json.loads((folder / ".cursor/mcp.json").read_text())["mcpServers"]["my-server"]
        self.assertEqual(entry["headers"]["Authorization"], "Bearer new-key-0123456789")

    def test_the_users_own_server_is_left_alone(self):
        folder = folder_for("cursor")
        path = folder / ".cursor/mcp.json"
        data = json.loads(path.read_text())
        data["mcpServers"]["my-server"] = {"url": "https://theirs.example/mcp"}
        path.write_text(json.dumps(data))
        changed, left = crewchat.sync_mcp_servers(folder, [remote()])
        self.assertEqual((changed, left), ([], [("cursor", "my-server")]))
        crewchat.sync_mcp_servers(folder, [])
        self.assertIn("theirs", path.read_text())

    def test_one_changed_by_hand_after_is_left_alone(self):
        folder = folder_for("cursor")
        path = folder / ".cursor/mcp.json"
        crewchat.sync_mcp_servers(folder, [remote()])
        data = json.loads(path.read_text())
        data["mcpServers"]["my-server"]["url"] = "https://edited.example/mcp"
        path.write_text(json.dumps(data))
        self.assertEqual(crewchat.sync_mcp_servers(folder, [remote()])[1], [("cursor", "my-server")])
        crewchat.sync_mcp_servers(folder, [])
        self.assertIn("edited", path.read_text())

    def test_a_file_git_tracks_is_left_alone(self):
        folder = folder_for("cursor")
        subprocess.run(["git", "init", "-q", str(folder)], check=True)
        subprocess.run(["git", "-C", str(folder), "add", "-f", ".cursor/mcp.json"], check=True)
        self.assertEqual(crewchat.sync_mcp_servers(folder, [remote()]), ([], []))
        self.assertNotIn("my-server", (folder / ".cursor/mcp.json").read_text())

    def test_the_ledger_is_kept_out_of_git(self):
        folder = folder_for("cursor")
        subprocess.run(["git", "init", "-q", str(folder)], check=True)
        crewchat.sync_mcp_servers(folder, [remote()])
        self.assertIn(crewchat.MCP_LEDGER, (folder / ".git/info/exclude").read_text())

    def test_disconnecting_takes_them_out(self):
        folder = folder_for("claude")
        crewchat.sync_mcp_servers(folder, [remote()])
        crewchat.disconnect_folder(folder)
        self.assertNotIn("my-server", (folder / ".mcp.json").read_text() if (folder / ".mcp.json").exists() else "")


class Cursor(unittest.TestCase):
    """Cursor's agent loads a server a folder lists only once it is approved there, and runs its tools without asking
    only when .cursor/cli.json allows them: crewchat does both for the servers it writes (owner's e2e test, 11 Nov)."""

    def test_its_tools_run_without_asking_only_when_the_owner_said_so(self):
        folder = folder_for("cursor")
        cli = folder / ".cursor/cli.json"
        crewchat.sync_mcp_servers(folder, [remote()])
        self.assertNotIn("my-server", cli.read_text())  # its tools ask
        changed, _ = crewchat.sync_mcp_servers(folder, [remote(allow=True)])
        self.assertIn(".cursor/cli.json", changed)
        permissions = json.loads(cli.read_text())["permissions"]
        self.assertIn("Mcp(my-server:*)", permissions["allow"])
        self.assertIn("Mcp(crewchat:hub_send)", permissions["allow"])  # crewchat's own stay
        self.assertEqual(permissions["deny"], [])  # Cursor's agent refuses a cli.json without one
        self.assertEqual(crewchat.sync_mcp_servers(folder, [remote(allow=True)])[0], [])  # up to date
        crewchat.sync_mcp_servers(folder, [remote()])
        self.assertNotIn("my-server", cli.read_text())
        crewchat.sync_mcp_servers(folder, [remote(allow=True)])
        crewchat.sync_mcp_servers(folder, [])  # taken out: so is its rule
        self.assertNotIn("my-server", cli.read_text())

    def test_the_servers_crewchat_wrote_are_the_ones_approved(self):
        folder = folder_for("cursor")
        path = folder / ".cursor/mcp.json"
        crewchat.sync_mcp_servers(folder, [remote()])
        crewchat.sync_device_server(folder, True)
        crewchat.sync_jira_server(folder, True)
        crewchat.sync_browser_server(folder, "bridge-token-0123")
        data = json.loads(path.read_text())
        data["mcpServers"]["their-own"] = {"url": "https://theirs.example/mcp"}  # the user's: theirs to approve
        path.write_text(json.dumps(data))
        names = [name for name, _ in crewchat.cursor_servers(folder)]
        self.assertEqual(names[0], "crewchat")
        self.assertEqual(sorted(names), ["atlassian", "browser", "crewchat", "mobile", "my-server"])
        calls = []

        def approve(again=False):
            run = lambda command, **kw: calls.append(command[1:]) or mock.Mock(returncode=0)  # noqa: E731
            with mock.patch.object(crewchat.subprocess, "run", run):
                return crewchat.approve_cursor_servers("/x/cursor-agent", folder, again=again)
        self.assertEqual(approve(again=True), names)
        self.assertEqual(calls, [["mcp", "enable", n] for n in names])
        del calls[:]
        self.assertEqual(approve(), [])  # nothing new since: no call (before each turn of a background agent)
        self.assertEqual(calls, [])
        crewchat.sync_device_server(folder, False)
        crewchat.sync_device_server(folder, True)  # the same again: still approved
        self.assertEqual(approve(), [])
        crewchat.sync_browser_server(folder, "a-new-bridge-token")  # changed: approved again
        self.assertIn("browser", approve())
        self.assertNotIn(["mcp", "enable", "their-own"], calls)

    def test_an_approval_that_fails_is_said_and_tried_again(self):
        # Two-machine test (11 Nov): on Windows a background Cursor agent never had crewchat's tools; the approval had
        # failed without a word, and was taken as done, so no later turn tried it again.
        folder = folder_for("cursor")
        results = {"crewchat": mock.Mock(returncode=1, stdout="", stderr="Error: could not approve crewchat\n")}
        calls = []

        def run(command, **kw):
            calls.append(kw.get("stdin"))
            return results.get(command[-1], mock.Mock(returncode=0, stdout="", stderr=""))
        err = io.StringIO()
        with mock.patch.object(crewchat.subprocess, "run", run), mock.patch.object(crewchat.sys, "stderr", err):
            self.assertEqual(crewchat.approve_cursor_servers("/x/cursor-agent", folder), [])
            self.assertIn("did not approve the MCP server crewchat", err.getvalue())
            self.assertIn("could not approve crewchat", err.getvalue())
            self.assertTrue(calls and all(s == crewchat.subprocess.DEVNULL for s in calls))
            del calls[:]
            results.clear()
            self.assertEqual(crewchat.approve_cursor_servers("/x/cursor-agent", folder), [])  # not at once (below)
            self.assertEqual(calls, [])
            later = time.time() + crewchat.CURSOR_RETRY + 1
            with mock.patch.object(crewchat.time, "time", lambda: later):
                self.assertEqual(crewchat.approve_cursor_servers("/x/cursor-agent", folder), ["crewchat"])  # tried again
                self.assertEqual(crewchat.approve_cursor_servers("/x/cursor-agent", folder), [])  # then done

    def test_a_failed_approval_waits_and_the_others_are_kept(self):
        # Review of 12 Nov: one server that failed kept the folder's whole approval from being saved, so every later turn
        # of a background Cursor agent ran `cursor-agent mcp enable` for every server again (Node, up to 30 s each).
        folder = folder_for("cursor")
        crewchat.sync_device_server(folder, True)
        crewchat.sync_jira_server(folder, True)
        calls = []

        def run(command, **kw):
            calls.append(command[-1])
            return mock.Mock(returncode=1 if command[-1] == "mobile" else 0, stdout="", stderr="no")
        with mock.patch.object(crewchat.subprocess, "run", run), mock.patch.object(crewchat.sys, "stderr", io.StringIO()):
            self.assertEqual(crewchat.approve_cursor_servers("/x/cursor-agent", folder), ["crewchat", "atlassian"])
            self.assertEqual(sorted(calls), ["atlassian", "crewchat", "mobile"])
            del calls[:]
            for _ in range(3):  # the turns that follow: nothing runs
                self.assertEqual(crewchat.approve_cursor_servers("/x/cursor-agent", folder, wait=False), [])
            self.assertEqual(calls, [])
            later = time.time() + crewchat.CURSOR_RETRY + 1
            with mock.patch.object(crewchat.time, "time", lambda: later):
                crewchat.approve_cursor_servers("/x/cursor-agent", folder)
            self.assertEqual(calls, ["mobile"])  # only the one that failed, once its wait is over
            del calls[:]
            self.assertEqual(crewchat.approve_cursor_servers("/x/cursor-agent", folder, again=True),
                             ["crewchat", "atlassian"])  # an agent's start tries everything at once
            self.assertEqual(sorted(calls), ["atlassian", "crewchat", "mobile"])

    def test_approvals_asked_at_once_run_once_on_one_worker(self):
        # Review of 12 Nov: at server start three callers (upgrade_folders, sync_mcp, sync_mobile) each started a thread
        # approving the same folders at once. Now they queue on one worker, a folder once, one approval at a time.
        folder = folder_for("cursor")
        crewchat.sync_device_server(folder, True)
        calls, running, most = [], [0], [0]
        lock = threading.Lock()

        def run(command, **kw):
            with lock:
                running[0] += 1
                most[0] = max(most[0], running[0])
            time.sleep(0.1)
            with lock:
                running[0] -= 1
                calls.append(command[-1])
            return mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(crewchat, "cursor_exe", lambda root=None: "/x/cursor-agent"), \
                mock.patch.object(crewchat.subprocess, "run", run):
            threads = [crewchat.approve_cursor_folders([folder]) for _ in range(3)]
            self.assertEqual(len({id(t) for t in threads}), 1)  # one worker
            threads[0].join(10)
        self.assertEqual(sorted(calls), ["crewchat", "mobile"])  # each once
        self.assertEqual(most[0], 1)

    def test_a_turn_does_not_wait_for_an_approval_running_in_its_folder(self):
        # A background Cursor agent's turn approves in the server's 5-second look at its agents: while the worker approves
        # in its folder (what the turn needs), the turn goes on at once instead of waiting behind it.
        folder = folder_for("cursor")
        crewchat.sync_device_server(folder, True)
        started, release, calls = threading.Event(), threading.Event(), []

        def run(command, **kw):
            calls.append(command[-1])
            started.set()
            release.wait(10)
            return mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch.object(crewchat.subprocess, "run", run):
            worker = threading.Thread(target=crewchat.approve_cursor_servers, args=("/x/cursor-agent", folder))
            worker.start()
            self.assertTrue(started.wait(10))
            began = time.time()
            self.assertEqual(crewchat.approve_cursor_servers("/x/cursor-agent", folder, wait=False), [])
            self.assertLess(time.time() - began, 1)
            release.set()
            worker.join(10)
        self.assertEqual(sorted(calls), ["crewchat", "mobile"])


    def test_every_connected_folder_gets_them_approved_not_only_where_an_agent_starts(self):
        # Owner's e2e test (11 Nov): in a folder where no agent was started from the page, `cursor-agent mcp list` showed
        # even crewchat "not loaded (needs approval)". Joining approves; so does a server crewchat writes later.
        calls = []
        run = lambda command, **kw: calls.append((command[1:], kw.get("cwd"))) or mock.Mock(returncode=0)  # noqa: E731
        with mock.patch.object(crewchat, "cursor_exe", lambda root=None: "/x/cursor-agent"):
            folder = Path(tempfile.mkdtemp(prefix="mcp-join-", dir=TMP)).resolve()
            real = crewchat.approve_cursor_servers

            def approve(exe, f, root=None, again=False):
                with mock.patch.object(crewchat.subprocess, "run", run):
                    return real(exe, f, root, again)
            with mock.patch.object(crewchat, "approve_cursor_servers", approve):
                crewchat.install_client("cursor", folder, "http://127.0.0.1:9", "tok")
                self.assertIn((["mcp", "enable", "crewchat"], str(folder)), calls)
                del calls[:]
                crewchat.sync_device_server(folder, True)  # turned on later: approved without an agent starting
                crewchat.approve_cursor_folders([folder, folder.parent / "not-there"], wait=True)
                self.assertEqual([c for c, _ in calls], [["mcp", "enable", "mobile"]])  # crewchat's was approved already
                del calls[:]
                crewchat.approve_cursor_folders([folder], wait=True)  # nothing new
                self.assertEqual(calls, [])
                claude = folder_for("claude")  # not connected for Cursor: nothing to approve
                self.assertIsNone(crewchat.approve_cursor_folders([claude]))
                self.assertEqual(calls, [])
                crewchat.sync_browser_server(folder, "bridge-token-0123")
                thread = crewchat.approve_cursor_folders([folder])  # at server start: in a thread of its own
                thread.join(10)
                self.assertIn((["mcp", "enable", "browser"], str(folder)), calls)


class Codex(unittest.TestCase):
    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="codex-", dir=TMP))
        patcher = mock.patch.dict(os.environ, {"CODEX_HOME": str(self.home)})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_its_block_holds_the_servers_ticked_for_codex(self):
        config = self.home / "config.toml"
        config.write_text('model = "x"\n')
        local = crewchat.mcp_server("db", command="npx", args=["-y", "db-mcp"], env={"T": "v"})
        self.assertTrue(crewchat.sync_codex_mcp([remote(), local, remote(name="only-claude", tools=["claude"])]))
        text = config.read_text()
        self.assertIn('[mcp_servers."my-server"]\nurl = "http://localhost:8792/mcp"\n'
                      'http_headers = { "Authorization" = "%s" }' % KEY, text)
        self.assertIn('[mcp_servers."db"]\ncommand = "npx"\nargs = ["-y", "db-mcp"]\nenv = { "T" = "v" }', text)
        self.assertNotIn("only-claude", text)
        self.assertTrue(text.startswith('model = "x"\n'))
        if os.name != "nt":  # Windows has no file modes
            self.assertFalse(os.stat(config).st_mode & 0o077)
        self.assertFalse(crewchat.sync_codex_mcp([remote(), local]))
        self.assertTrue(crewchat.sync_codex_mcp([]))
        self.assertEqual(config.read_text().strip(), 'model = "x"')

    def test_no_codex_settings_are_made_where_codex_was_never_set_up(self):
        # Windows run: a server added for one project made ~/.codex, on a machine where Codex was never used.
        os.environ["CODEX_HOME"] = str(self.home / "not-there")
        self.assertFalse(crewchat.sync_codex_mcp([remote()]))
        self.assertFalse((self.home / "not-there").exists())
        (self.home / "not-there").mkdir()  # Codex set up since: its servers are written
        self.assertTrue(crewchat.sync_codex_mcp([remote()]))
        self.assertIn('[mcp_servers."my-server"]', (self.home / "not-there" / "config.toml").read_text())

    def test_only_a_project_with_a_folder_connected_for_codex_reaches_codex(self):
        # B7: Codex's settings are for the whole machine; a project used only with other tools put its servers into
        # every Codex session all the same.
        base = Path(tempfile.mkdtemp(prefix="codex-home-", dir=TMP)).resolve()
        web_folder, api_folder = (Path(tempfile.mkdtemp(prefix=p, dir=TMP)).resolve() for p in ("web-", "api-"))
        web, _ = crewchat.mcp_server("web-db", command="npx", args=["web-db"])
        api, _ = crewchat.mcp_server("api-db", command="npx", args=["api-db"])
        crewchat.save_config({"project": "web", "port": 9, "mcp_servers": [web],
                              "folders": {str(web_folder): "mac", str(api_folder): "mac"},
                              "folder_chats": {str(api_folder): "api"}}, base)
        (base / crewchat.PROJECTS_DIR / "api").mkdir(parents=True)
        crewchat.save_config({"project": "api", "port": 9, "mcp_servers": [api]}, base / crewchat.PROJECTS_DIR / "api")
        entry = lambda clients: {"url": "http://127.0.0.1:9", "token": "t", "clients": clients}  # noqa: E731
        (base / crewchat.REGISTRY_FILE).write_text(json.dumps({"folders": {
            str(web_folder): entry(["claude", "cursor"]), str(api_folder): entry(["claude", "codex"])}}))
        self.assertEqual([s["name"] for s, _ in crewchat.mcp_all_servers(base)], ["api-db"])
        self.assertTrue(crewchat.sync_codex_mcp(crewchat.mcp_all_servers(base), base))
        text = (self.home / "config.toml").read_text()
        self.assertIn('[mcp_servers."api-db"]', text)
        self.assertNotIn("web-db", text)
        (base / crewchat.REGISTRY_FILE).write_text(json.dumps({"folders": {str(api_folder): entry(["claude"])}}))
        self.assertTrue(crewchat.sync_codex_mcp(crewchat.mcp_all_servers(base), base))  # no Codex folder left: out
        self.assertNotIn("api-db", (self.home / "config.toml").read_text())
        # The card says Codex's are machine-wide.
        self.assertIn("every Codex session on this machine", crewchat.CHAT_PAGE)

    def test_a_codex_server_of_the_users_own_is_left_out(self):
        config = self.home / "config.toml"
        config.write_text('[mcp_servers.my-server]\nurl = "https://theirs.example/"\n')
        crewchat.sync_codex_mcp([remote()])
        self.assertEqual(config.read_text().count("my-server"), 1)

    def test_each_chat_home_keeps_its_own_block(self):
        # Codex's settings are one file for the machine: a test home's servers never replace or take out the main
        # chat's, and its `crewchat mcp remove` (or uninstall) takes out only its own.
        config = self.home / "config.toml"
        main, test = Path(TMP) / "main-home", Path(TMP) / "e2e home"
        self.assertTrue(crewchat.sync_codex_mcp([remote(name="mine")], main))
        self.assertTrue(crewchat.sync_codex_mcp([remote(name="e2e-test"), remote(name="mine", url="https://other.example/")], test))
        text = config.read_text()
        self.assertEqual(text.count('[mcp_servers."mine"]'), 1)  # the main chat's, not the test home's
        self.assertIn('[mcp_servers."e2e-test"]', text)
        self.assertNotIn("other.example", text)
        self.assertIn(json.dumps(str(test)), text)
        self.assertEqual(sorted(crewchat.mcp_codex_servers(text)), [])  # neither is a server of the user's own
        self.assertFalse(crewchat.sync_codex_mcp([remote(name="mine")], main))  # the test home's block is not its
        self.assertTrue(crewchat.sync_codex_mcp([], test))
        text = config.read_text()
        self.assertNotIn("e2e-test", text)
        self.assertIn('[mcp_servers."mine"]', text)
        self.assertTrue(crewchat.sync_codex_mcp([], main))
        self.assertNotIn("mcp_servers", config.read_text())

    def test_the_old_block_is_the_default_homes(self):
        # Before 2.0.0 the block named no chat home: the default home (~/.crewchat) takes it over, another leaves it.
        user = Path(tempfile.mkdtemp(prefix="user-", dir=TMP))
        config = self.home / "config.toml"
        old = "%s\n[mcp_servers.\"e2e-test\"]\nurl = \"http://localhost:1/\"\n%s\n" % (
            crewchat.MCP_CODEX_BEGIN, crewchat.MCP_CODEX_END)
        config.write_text('model = "x"\n\n' + old)
        with mock.patch.dict(os.environ, {"HOME": str(user), "USERPROFILE": str(user)}):
            self.assertTrue(crewchat.sync_codex_mcp([remote(name="theirs")], Path(TMP) / "a test home"))
            self.assertIn(old, config.read_text())  # left alone
            self.assertTrue(crewchat.sync_codex_mcp([remote(name="mine")], user / ".crewchat"))
            text = config.read_text()
        self.assertNotIn("e2e-test", text)
        self.assertEqual(text.count(crewchat.MCP_CODEX_BEGIN), 2)  # the default home's and the test home's
        self.assertIn('[mcp_servers."mine"]', text)
        self.assertIn('[mcp_servers."theirs"]', text)
        self.assertTrue(text.startswith('model = "x"\n'))


class FakeServer(http.server.BaseHTTPRequestHandler):
    """A remote MCP server on this machine: it wants KEY, or (oauth) points at its authorization server."""
    oauth = False

    def log_message(self, *args):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if self.headers.get("Authorization") != KEY:
            self.send_response(401)
            self.send_header("WWW-Authenticate", 'Bearer resource_metadata="http://localhost/.well-known/x"'
                             if self.oauth else 'Bearer realm="api"')
            self.end_headers()
            return
        if "id" not in body:
            self.send_response(202)
            self.end_headers()
            return
        result = {"initialize": {"protocolVersion": "2025-06-18"},
                  "tools/list": {"tools": [{"name": "search"}, {"name": "fetch"}]}}[body["method"]]
        raw = ("data: %s\n\n" % json.dumps({"jsonrpc": "2.0", "id": body["id"], "result": result})).encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Mcp-Session-Id", "s1")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)


class Asking(unittest.TestCase):
    def serve(self, oauth=False):
        handler = type("H", (FakeServer,), {"oauth": oauth})
        server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        return "http://localhost:%d/mcp" % server.server_address[1]

    def test_the_sign_in_probe(self):
        self.assertEqual(crewchat.mcp_sign_in(self.serve(oauth=True)), "oauth")
        self.assertEqual(crewchat.mcp_sign_in(self.serve()), "key")

    def test_a_remote_servers_tools(self):
        url = self.serve()
        self.assertEqual(crewchat.mcp_remote_tools(url, {"Authorization": KEY}), ["search", "fetch"])
        with self.assertRaisesRegex(crewchat.HubError, "refused the sign-in"):
            crewchat.mcp_remote_tools(url, {"Authorization": "Bearer wrong"})

    def test_a_local_servers_tools(self):
        script = Path(TMP) / "fake_stdio_mcp.py"
        script.write_text(textwrap.dedent('''
            import json, os, sys
            for line in sys.stdin:
                msg = json.loads(line)
                if "id" not in msg:
                    continue
                result = {"initialize": {"protocolVersion": "2025-06-18"},
                          "tools/list": {"tools": [{"name": "q-" + os.environ.get("WHO", "")}]}}[msg["method"]]
                print(json.dumps({"jsonrpc": "2.0", "id": msg["id"], "result": result}), flush=True)
        '''))
        self.assertEqual(crewchat.mcp_local_tools(sys.executable, [str(script)], {"WHO": "me"}), ["q-me"])
        with self.assertRaisesRegex(crewchat.HubError, "not on this machine's PATH"):
            crewchat.mcp_local_tools("no-such-command-here", [])


class Project(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="mcp-hub-", dir=TMP))
        crewchat.save_config({"project": "web", "port": 9}, self.root)
        self.hub = crewchat.Hub(crewchat.Roster(self.root))
        self.addCleanup(self.hub.events.close)
        self.folder = folder_for("claude")
        patcher = mock.patch.object(self.hub, "project_folders", return_value=[self.folder])
        patcher.start()
        self.addCleanup(patcher.stop)
        codex = mock.patch.dict(os.environ, {"CODEX_HOME": str(Path(tempfile.mkdtemp(dir=TMP)))})
        codex.start()
        self.addCleanup(codex.stop)

    def servers(self):
        return json.loads((self.folder / ".mcp.json").read_text())["mcpServers"]

    def test_pasted_json_reaches_the_folder_and_the_chat_is_told(self):
        block = json.loads(json.dumps(PASTED))
        block["mcpServers"]["my-server"]["headers"]["Authorization"] = KEY
        out = self.hub.add_mcp_json(json.dumps(block), probe=False)
        self.assertEqual(out["added"], ["my-server"])
        self.assertEqual(self.servers()["my-server"]["headers"], {"Authorization": KEY})
        self.assertNotIn(KEY, json.dumps(self.hub.mcp_servers()))  # never back to the page
        self.assertIn("added the MCP server my-server", self.hub.messages[-1]["text"])
        self.assertIn("starts again", self.hub.messages[-1]["text"])
        self.assertNotIn(KEY, json.dumps(self.hub.messages))
        guide = (self.folder / ".claude/skills/crewchat/SKILL.md").read_text()
        self.assertIn("`my-server`", guide)
        self.assertNotIn(KEY, guide)
        self.assertIn(KEY.split()[1].encode(), crewchat.stored_tokens([self.root]))  # an agent may not share it
        self.hub.remove_mcp_server("my-server")
        self.assertNotIn("my-server", self.servers())
        self.assertNotIn("mcp_secrets", crewchat.load_config(self.root))

    def test_a_placeholder_adds_nothing(self):
        with self.assertRaisesRegex(crewchat.HubError, "<YOUR_API_KEY>"):
            self.hub.add_mcp_json(json.dumps(PASTED), probe=False)
        self.assertEqual(self.hub.mcp_servers()["servers"], [])

    def test_an_empty_key_keeps_the_one_it_had(self):
        self.hub.set_mcp_server({"name": "my-server", "url": "https://a.example/mcp", "headers": {"Authorization": KEY}},
                                probe=False)
        self.hub.set_mcp_server({"name": "my-server", "url": "https://b.example/mcp", "headers": {"Authorization": ""}},
                                probe=False)
        entry = self.servers()["my-server"]
        self.assertEqual((entry["url"], entry["headers"]["Authorization"]), ("https://b.example/mcp", KEY))
        self.assertIn("changed the MCP server", self.hub.messages[-1]["text"])

    def test_a_folder_connected_later_gets_them_at_start(self):
        self.hub.set_mcp_server({"name": "db", "command": "npx", "args": ["db-mcp"]}, probe=False)
        later = folder_for("cursor")
        self.hub.sync_mcp([later])
        self.assertIn("db", json.loads((later / ".cursor/mcp.json").read_text())["mcpServers"])


class Linked(unittest.TestCase):
    """E4: a server's description, never its key, reaches the project's linked machines as a plan message."""

    def hub(self, client="claude"):
        root = Path(tempfile.mkdtemp(prefix="mcp-linked-", dir=TMP))
        crewchat.save_config({"project": "web", "port": 9}, root)
        hub = crewchat.Hub(crewchat.Roster(root))
        self.addCleanup(hub.events.close)
        folder = folder_for(client)
        patcher = mock.patch.object(hub, "project_folders", return_value=[folder])
        patcher.start()
        self.addCleanup(patcher.stop)
        return hub, folder

    def setUp(self):
        codex = mock.patch.dict(os.environ, {"CODEX_HOME": str(Path(tempfile.mkdtemp(dir=TMP)))})
        codex.start()
        self.addCleanup(codex.stop)
        self.a, self.a_folder = self.hub()
        self.b, self.b_folder = self.hub()

    def carry(self, since=0):
        """Every plan message of a's after since to b, as cloud sync carries them; returns them."""
        sent = [m for m in self.a.messages if m["kind"] == "plan" and m.get("mcp") and m["seq"] > since]
        for m in sent:
            self.b.ingest(dict(m, origin="mac-a"))
        return sent

    def servers(self, folder):
        try:
            return json.loads((folder / ".mcp.json").read_text())["mcpServers"]
        except (OSError, KeyError):
            return {}

    def row(self, hub, name):
        return next((r for r in hub.mcp_servers()["servers"] if r["name"] == name), None)

    def test_a_description_reaches_a_second_machine_without_its_key(self):
        self.a.set_mcp_server({"name": "sentry", "url": "https://mcp.sentry.example/mcp",
                               "headers": {"Authorization": KEY}}, probe=False)
        sent = self.carry()
        self.assertEqual(len(sent), 1)
        self.assertNotIn(KEY.split()[1], json.dumps(sent))
        self.assertNotIn(KEY.split()[1], (self.a.home / "messages.jsonl").read_text())
        row = self.row(self.b, "sentry")
        self.assertEqual((row["url"], row["headers"], row["missing_keys"]),
                         ("https://mcp.sentry.example/mcp", ["Authorization"], ["Authorization"]))
        self.assertNotIn("sentry", self.servers(self.b_folder))  # not written without its key
        self.assertNotIn("mcp_secrets", crewchat.load_config(self.b.roster.root))
        # The owner gives b its own key on b's card: written there, and nothing changes back on a.
        other = "Bearer sk-other-machine-0123456789"
        self.b.set_mcp_server({"name": "sentry", "url": "https://mcp.sentry.example/mcp",
                               "headers": {"Authorization": other}}, probe=False)
        self.assertEqual(self.servers(self.b_folder)["sentry"]["headers"]["Authorization"], other)
        self.assertEqual(self.row(self.b, "sentry")["missing_keys"], [])
        for m in self.b.messages:
            if m["kind"] == "plan" and m.get("mcp"):
                self.assertNotIn("sk-other", json.dumps(m))
                self.a.ingest(dict(m, origin="mac-b"))
        self.assertEqual(self.servers(self.a_folder)["sentry"]["headers"]["Authorization"], KEY)

    def test_one_with_no_key_is_written_there_and_a_missing_command_is_said(self):
        self.a.set_mcp_server({"name": "db", "command": "no-such-mcp-command-here", "args": ["--ro"]}, probe=False)
        self.carry()
        self.assertEqual(self.servers(self.b_folder)["db"], {"command": "no-such-mcp-command-here", "args": ["--ro"]})
        row = self.row(self.b, "db")
        self.assertEqual(row["no_command"], "no-such-mcp-command-here")
        items = self.b.mcp_readiness("claude")
        self.assertEqual(len(items), 1)
        self.assertIn("no-such-mcp-command-here", items[0]["text"])
        self.assertIn("PATH", items[0]["text"])
        self.assertEqual(self.b.mcp_readiness("gemini"), items)  # every tool it is for
        self.a.set_mcp_server({"name": "db", "command": "no-such-mcp-command-here", "args": ["--ro"], "tools": ["cursor"]},
                              probe=False)
        self.carry()
        self.assertEqual(self.b.mcp_readiness("claude"), [])

    def test_a_removal_reaches_it_and_an_older_change_does_not_win(self):
        self.a.set_mcp_server({"name": "db", "command": "npx", "args": ["db-mcp"]}, probe=False)
        first = self.carry()
        self.a.remove_mcp_server("db")
        self.carry(first[-1]["seq"])
        self.assertIsNone(self.row(self.b, "db"))
        self.assertNotIn("db", self.servers(self.b_folder))
        late = dict(first[0], id="late-copy", origin="mac-a")  # an older description, arriving after
        self.b.ingest(late)
        self.assertIsNone(self.row(self.b, "db"))

    def test_a_server_on_this_machine_stays_here_unless_the_owner_shares_it(self):
        self.a.set_mcp_server({"name": "local-web", "url": "http://localhost:8792/mcp"}, probe=False)
        self.assertEqual(self.carry(), [])
        self.assertIn("localhost", self.row(self.a, "local-web")["kept_here"])
        self.a.set_mcp_server({"name": "local-web", "url": "http://localhost:8792/mcp", "share": True}, probe=False)
        self.carry()
        self.assertEqual(self.row(self.b, "local-web")["url"], "http://localhost:8792/mcp")
        self.assertEqual(self.row(self.a, "local-web")["kept_here"], "")

    def test_a_key_in_the_command_line_is_never_shared(self):
        for args in (["--token", "sk-abcdefghijklmnopqrstuv"], ["postgresql://me:hunter2pass@db.example/app"],
                     ["--api-key=abcdef0123456789"]):
            self.a.set_mcp_server({"name": "db", "command": "npx", "args": args}, probe=False)
            self.assertEqual([m for m in self.a.messages if m["kind"] == "plan" and m.get("mcp")
                              and "removed" not in m["mcp"]], [], args)
            self.assertIn("environment variable", self.row(self.a, "db")["kept_here"])
            self.a.remove_mcp_server("db")
        self.a.set_mcp_server({"name": "db", "command": "npx", "args": ["db-mcp", "--ro"], "env": {"DB_PASSWORD": "hunter2pass"}},
                              probe=False)
        self.assertNotIn("hunter2pass", json.dumps(self.carry()))
        self.assertEqual(self.row(self.b, "db")["missing_keys"], ["DB_PASSWORD"])

    def test_one_kept_here_after_it_was_shared_goes_from_the_others_only(self):
        self.a.set_mcp_server({"name": "db", "command": "npx", "args": ["db-mcp"]}, probe=False)
        seq = self.carry()[-1]["seq"]
        self.assertIsNotNone(self.row(self.b, "db"))
        self.a.set_mcp_server({"name": "db", "command": "npx", "args": ["db-mcp", "--token", "abcdefgh12345"]}, probe=False)
        self.carry(seq)
        self.assertIsNone(self.row(self.b, "db"))
        self.assertEqual(self.servers(self.a_folder)["db"]["args"], ["db-mcp", "--token", "abcdefgh12345"])
        self.assertIn("environment variable", self.row(self.a, "db")["kept_here"])
        # A server of b's own that b keeps to itself is not changed from a.
        self.b.set_mcp_server({"name": "db", "command": "npx", "args": ["db-mcp"], "share": False}, probe=False)
        self.a.set_mcp_server({"name": "db", "command": "npx", "args": ["other-mcp"]}, probe=False)
        self.carry(seq)
        self.assertEqual(self.row(self.b, "db")["args"], ["db-mcp"])
        self.assertEqual(self.row(self.b, "db")["kept_here"], "you keep it on this machine")

    def test_the_description_is_checked_on_arrival(self):
        bad = {"id": "x1", "ts": 9e9, "from": crewchat.OWNER, "to": "all", "text": "", "kind": "plan", "origin": "mac-a",
               "mcp": json.dumps({"name": "crewchat", "kind": "local", "command": "sh", "args": [], "tools": ["claude"]})}
        self.b.ingest(bad)
        self.assertEqual(self.b.mcp_servers()["servers"], [])
        bad = dict(bad, id="x2", mcp=json.dumps({"name": "x", "kind": "remote", "url": "http://evil.example/mcp",
                                                  "tools": ["claude"]}))
        self.b.ingest(bad)
        self.assertEqual(self.b.mcp_servers()["servers"], [])
        bad = dict(bad, id="x3", mcp=json.dumps({"name": "x", "kind": "local", "command": "sh", "tools": ["claude"]}))
        bad["from"] = "claude-alpha"  # only the owner's word changes the project's servers
        self.b.ingest(bad)
        self.assertEqual(self.b.mcp_servers()["servers"], [])


class FromATool(unittest.TestCase):
    """E4: "Add from a tool" offers the servers already set up in Claude Code, Cursor or Codex, without their keys."""

    def setUp(self):
        self.user = Path(tempfile.mkdtemp(prefix="mcp-user-", dir=TMP))
        patcher = mock.patch.object(crewchat, "user_home", lambda: self.user)
        patcher.start()
        self.addCleanup(patcher.stop)
        env = mock.patch.dict(os.environ, {"CODEX_HOME": str(self.user / ".codex")})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("CLAUDE_CONFIG_DIR", None)
        self.root = Path(tempfile.mkdtemp(prefix="mcp-from-", dir=TMP))
        crewchat.save_config({"project": "web", "port": 9}, self.root)
        self.hub = crewchat.Hub(crewchat.Roster(self.root))
        self.addCleanup(self.hub.events.close)
        self.folder = folder_for("claude")
        patcher = mock.patch.object(self.hub, "project_folders", return_value=[self.folder])
        patcher.start()
        self.addCleanup(patcher.stop)
        (self.user / ".claude.json").write_text(json.dumps({
            "mcpServers": {"linear": {"type": "sse", "url": "https://mcp.linear.app/sse"},
                           "crewchat-old": {"command": "uvx", "args": ["crewchat", "stdio"]}},
            "projects": {str(self.folder): {"mcpServers": {"pg": {
                "command": "npx", "args": ["-y", "pg-mcp"], "env": {"PGPASSWORD": "pg-secret-value-123"}}}}}}))
        mcp = json.loads((self.folder / ".mcp.json").read_text())
        mcp["mcpServers"]["github"] = {"type": "http", "url": "https://api.githubcopilot.example/mcp/",
                                       "headers": {"Authorization": KEY}}
        (self.folder / ".mcp.json").write_text(json.dumps(mcp))
        (self.user / ".cursor").mkdir()
        (self.user / ".cursor" / "mcp.json").write_text(json.dumps({"mcpServers": {
            "linear": {"url": "https://mcp.linear.app/sse"},
            "todo": {"url": "https://todo.example/mcp", "headers": {"X-Key": "<YOUR_KEY>"}}}}))
        (self.user / ".codex").mkdir()
        (self.user / ".codex" / "config.toml").write_text(textwrap.dedent('''\
            model = "gpt-5"

            [mcp_servers.sentry]
            command = "npx"
            args = [
              "-y", "@sentry/mcp-server",
            ]
            [mcp_servers.sentry.env]
            SENTRY_TOKEN = "sentry-secret-value-456"

            [mcp_servers."docs"]
            url = 'https://docs.example/mcp'
            http_headers = { "X-Api-Key" = "docs-secret-789" }
            enabled = true
            ''') + crewchat.mcp_codex_block([remote(name="mine", url="https://mine.example/mcp")]))

    def found(self):
        return {(f["name"]): f for f in self.hub.mcp_found()["found"]}

    def test_the_tools_servers_are_offered_without_their_keys(self):
        out = self.hub.mcp_found()
        text = json.dumps(out)
        for secret in ("pg-secret-value-123", KEY.split()[1], "sentry-secret-value-456", "docs-secret-789"):
            self.assertNotIn(secret, text)
        found = self.found()
        self.assertEqual(sorted(found), ["docs", "github", "linear", "pg", "sentry", "todo"])  # not crewchat's own
        self.assertEqual(sorted(found["linear"]["tools"]), ["Claude Code", "Cursor"])  # the same server in two tools
        self.assertEqual(found["pg"]["keys"], ["PGPASSWORD"])
        self.assertEqual((found["sentry"]["command"], found["sentry"]["args"], found["sentry"]["keys"]),
                         ("npx", ["-y", "@sentry/mcp-server"], ["SENTRY_TOKEN"]))
        self.assertEqual((found["docs"]["url"], found["docs"]["keys"]), ("https://docs.example/mcp", ["X-Api-Key"]))
        self.assertEqual(found["github"]["keys"], ["Authorization"])
        self.assertIn("<YOUR_KEY>", found["todo"]["problem"])
        self.assertEqual(found["linear"]["problem"], "")

    def test_adding_one_brings_its_key_to_every_tool_here(self):
        found = self.found()
        self.hub.mcp_import("sentry", found["sentry"]["id"])
        cursor_folder = folder_for("cursor")
        self.hub.sync_mcp([cursor_folder])
        entry = json.loads((cursor_folder / ".cursor/mcp.json").read_text())["mcpServers"]["sentry"]
        self.assertEqual(entry["env"], {"SENTRY_TOKEN": "sentry-secret-value-456"})
        self.assertNotIn("sentry-secret-value-456", json.dumps(self.hub.mcp_servers()))
        self.assertNotIn("sentry", self.found())  # offered no more: it is the project's now
        with self.assertRaisesRegex(crewchat.HubError, "YOUR_KEY"):
            self.hub.mcp_import("todo", found["todo"]["id"])
        with self.assertRaisesRegex(crewchat.HubError, "no longer"):
            self.hub.mcp_import("pg", "not-its-id")

    def test_codex_settings_are_read(self):
        found = crewchat.mcp_codex_servers((self.user / ".codex" / "config.toml").read_text())
        self.assertEqual(found["sentry"], {"command": "npx", "args": ["-y", "@sentry/mcp-server"],
                                           "env": {"SENTRY_TOKEN": "sentry-secret-value-456"}})
        self.assertEqual(found["docs"], {"url": "https://docs.example/mcp", "http_headers": {"X-Api-Key": "docs-secret-789"},
                                         "enabled": True})
        self.assertNotIn("mine", found)  # crewchat's own block is not the user's


if __name__ == "__main__":
    unittest.main()
