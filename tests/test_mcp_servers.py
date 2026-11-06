"""The owner's own MCP servers (E1 to E3): checked, written for every tool in every connected folder, and asked."""
import http.server
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import threading
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

    def test_a_codex_server_of_the_users_own_is_left_out(self):
        config = self.home / "config.toml"
        config.write_text('[mcp_servers.my-server]\nurl = "https://theirs.example/"\n')
        crewchat.sync_codex_mcp([remote()])
        self.assertEqual(config.read_text().count("my-server"), 1)


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


if __name__ == "__main__":
    unittest.main()
