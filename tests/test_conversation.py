"""Tests for the readers of the tools' conversation files (A2): each tool's records in a made-up file, turned into
entries (time, who, text)."""
import json
import os
import sqlite3
import sys
import tempfile
import time
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

TMP = tempfile.mkdtemp(prefix="crewchat-conv-test-")
os.environ["CREWCHAT_HOME"] = str(Path(TMP) / "home")

import crewchat  # noqa: E402  (after CREWCHAT_HOME is set)

UUID = "0199a213-81c0-7800-8aa1-bbab2a035a53"


def at(hhmm, day="2026-10-20"):
    """The epoch second of HH:MM (or HH:MM:SS) UTC on a day."""
    text = hhmm if hhmm.count(":") == 2 else hhmm + ":00"
    return datetime.fromisoformat("%sT%s+00:00" % (day, text)).timestamp()


def iso(hhmm, day="2026-10-20"):
    return "%sT%s.000Z" % (day, hhmm if hhmm.count(":") == 2 else hhmm + ":00")


def write_lines(path, records):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
    return path


class Home(unittest.TestCase):
    """A throwaway user home the readers look in, instead of the real one."""

    def setUp(self):
        self.home = Path(tempfile.mkdtemp(prefix="home-", dir=TMP))
        patches = [mock.patch.object(crewchat, "user_home", lambda: self.home),
                   mock.patch.dict(os.environ, {"CODEX_HOME": "", "CLAUDE_CONFIG_DIR": "", "QWEN_RUNTIME_DIR": "", "OPENCODE_DB": ""})]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def read(self, tool, place, since=None, **kw):
        return crewchat.read_conversation(tool, place, since, **kw)


class ClaudeCode(Home):
    def path(self):
        return self.home / ".claude" / "projects" / "x" / "s.jsonl"

    def test_messages_tools_and_results_in_order(self):
        path = write_lines(self.path(), [
            {"type": "user", "timestamp": iso("10:00"), "message": {"role": "user", "content": "run the tests"}},
            {"type": "assistant", "timestamp": iso("10:01"), "message": {"role": "assistant", "content": [
                {"type": "thinking", "thinking": "hmm"},
                {"type": "text", "text": "Running them."},
                {"type": "tool_use", "id": "t1", "name": "Bash", "input": {"command": "npm test"}}]}},
            {"type": "user", "timestamp": iso("10:02"), "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t1", "content": "3 passed"}]}},
            {"type": "user", "timestamp": iso("10:03"), "message": {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "t2", "content": [{"type": "text", "text": "a"},
                                                                          {"type": "text", "text": "b"}]}]}},
            {"type": "summary", "summary": "unknown record"},
            {"type": "file-history-snapshot", "snapshot": {}}])
        self.assertEqual(self.read("claude", path.resolve()), [
            (at("10:00"), "user", "run the tests"),
            (at("10:01"), "agent", "Running them."),
            (at("10:01"), "tool Bash", '{"command": "npm test"}'),
            (at("10:02"), "result", "3 passed"),
            (at("10:03"), "result", "a\nb")])

    def test_since_cuts_at_the_task(self):
        path = write_lines(self.path(), [
            {"type": "user", "timestamp": iso("10:00"), "message": {"role": "user", "content": "old question"}},
            {"type": "assistant", "timestamp": iso("10:00:30"), "message": {"role": "assistant", "content": "old answer"}},
            {"type": "user", "timestamp": iso("10:05"), "message": {"role": "user", "content": "new question"}},
            {"type": "assistant", "timestamp": iso("10:06"), "message": {"role": "assistant", "content": [
                {"type": "text", "text": "new answer"}]}}])
        got = self.read("claude", path.resolve(), at("10:02"))
        self.assertEqual([(who, text) for _, who, text in got], [("user", "new question"), ("agent", "new answer")])

    def test_times_with_other_spellings(self):
        for stamp in ("2026-10-20T10:00:00Z", "2026-10-20T10:00:00.5Z", "2026-10-20T10:00:00.123456789Z",
                      "2026-10-20T10:00:00+00:00", "2026-10-20T12:00:00+02:00", "2026-10-20T10:00:00"):
            self.assertAlmostEqual(crewchat.conversation_time(stamp), at("10:00"), delta=1, msg=stamp)
        for bad in (None, "", "yesterday", 5, [], "2026-13-45T99:00:00Z"):
            self.assertIsNone(crewchat.conversation_time(bad), bad)

    def test_a_link_swapped_in_after_the_hook_is_refused(self):
        path = write_lines(self.path(), [{"type": "user", "timestamp": iso("10:00"),
                                          "message": {"role": "user", "content": "hi"}}])
        place = crewchat.conversation_place("claude", {"path": str(path)}, self.home)
        self.assertEqual(place, path.resolve())  # accepted at hook time
        self.assertEqual(len(self.read("claude", place)), 1)
        secret = write_lines(self.home / ".ssh" / "keys.jsonl", [{"type": "user", "message": {"content": "key"}}])
        path.unlink()
        path.symlink_to(secret)  # swapped for a link to a file outside the folder
        with self.assertRaises(crewchat.ConversationUnavailable) as caught:
            self.read("claude", place)
        self.assertNotIn("key", caught.exception.reason)

    def test_a_link_to_another_file_in_the_folder_is_refused_too(self):
        path = write_lines(self.path(), [{"type": "user", "message": {"content": "mine"}}])
        other = write_lines(path.with_name("other.jsonl"), [{"type": "user", "message": {"content": "other"}}])
        place = crewchat.conversation_place("claude", {"path": str(path)}, self.home)
        path.unlink()
        path.symlink_to(other)
        with self.assertRaises(crewchat.ConversationUnavailable):
            self.read("claude", place)

    def test_a_fifo_is_refused_not_waited_on(self):
        fifo = self.path()
        fifo.parent.mkdir(parents=True)
        os.mkfifo(fifo)
        place = crewchat.conversation_place("claude", {"path": str(fifo)}, self.home)
        self.assertEqual(place, fifo.resolve())
        with self.assertRaises(crewchat.ConversationUnavailable):
            self.read("claude", place)

    def test_another_claude_account_needs_its_config(self):
        account = Path(tempfile.mkdtemp(prefix="work-claude-", dir=TMP))
        path = write_lines(account / "projects" / "x" / "s.jsonl", [
            {"type": "user", "timestamp": iso("10:00"), "message": {"role": "user", "content": "hi"}}])
        with self.assertRaises(crewchat.ConversationUnavailable):
            self.read("claude", path.resolve())
        config = {"claude_accounts": {"work": str(account)}}
        self.assertEqual(len(self.read("claude", path.resolve(), config=config)), 1)


class Codex(Home):
    def rollout(self, name="rollout-2026-10-20T10-00-00-%s.jsonl" % UUID, day=("2026", "10", "20")):
        return self.home / ".codex" / "sessions" / Path(*day) / name

    def test_response_items(self):
        path = write_lines(self.rollout(), [
            {"timestamp": iso("10:00"), "type": "session_meta", "payload": {"id": UUID}},
            {"timestamp": iso("10:00:01"), "type": "response_item", "payload": {
                "type": "message", "role": "user", "content": [{"type": "input_text", "text": "fix the bug"}]}},
            {"timestamp": iso("10:00:02"), "type": "response_item", "payload": {
                "type": "reasoning", "summary": [{"type": "summary_text", "text": "thinking"}]}},
            {"timestamp": iso("10:00:03"), "type": "response_item", "payload": {
                "type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "On it."}]}},
            {"timestamp": iso("10:00:04"), "type": "response_item", "payload": {
                "type": "function_call", "name": "shell", "call_id": "c1",
                "arguments": json.dumps({"command": ["ls", "-la"]})}},
            {"timestamp": iso("10:00:05"), "type": "response_item", "payload": {
                "type": "function_call_output", "call_id": "c1", "output": "total 0"}},
            {"timestamp": iso("10:00:06"), "type": "response_item", "payload": {
                "type": "custom_tool_call", "name": "apply_patch", "call_id": "c2", "input": "*** Begin Patch"}},
            {"timestamp": iso("10:00:07"), "type": "response_item", "payload": {
                "type": "custom_tool_call_output", "call_id": "c2", "output": "Done"}},
            {"timestamp": iso("10:00:08"), "type": "response_item", "payload": {
                "type": "local_shell_call", "action": {"type": "exec", "command": ["pwd"]}}},
            {"timestamp": iso("10:00:09"), "type": "event_msg", "payload": {"type": "token_count"}},
            {"timestamp": iso("10:00:10"), "type": "turn_context", "payload": {}}])
        self.assertEqual(self.read("codex", path.resolve()), [
            (at("10:00:01"), "user", "fix the bug"),
            (at("10:00:03"), "agent", "On it."),
            (at("10:00:04"), "tool shell", '{"command": ["ls", "-la"]}'),
            (at("10:00:05"), "result", "total 0"),
            (at("10:00:06"), "tool apply_patch", "*** Begin Patch"),
            (at("10:00:07"), "result", "Done"),
            (at("10:00:08"), "tool local_shell", '{"command": ["pwd"], "type": "exec"}')])

    def test_found_by_session_id(self):
        other = "0199a213-81c0-7800-8aa1-bbab2a035a54"
        old = write_lines(self.rollout(day=("2026", "10", "19")), [
            {"timestamp": iso("09:00"), "type": "response_item", "payload": {
                "type": "message", "role": "user", "content": [{"type": "input_text", "text": "old"}]}}])
        os.utime(old, (1000, 1000))
        write_lines(self.rollout(), [
            {"timestamp": iso("10:00"), "type": "response_item", "payload": {
                "type": "message", "role": "user", "content": [{"type": "input_text", "text": "new"}]}}])
        write_lines(self.rollout("rollout-2026-10-20T10-00-00-%s.jsonl" % other), [
            {"timestamp": iso("10:00"), "type": "response_item", "payload": {
                "type": "message", "role": "user", "content": [{"type": "input_text", "text": "other session"}]}}])
        self.assertEqual(crewchat._codex_file(UUID), self.rollout())
        self.assertEqual([t for _, _, t in self.read("codex", UUID)], ["new"])
        self.assertIsNone(crewchat._codex_file("0199a213-81c0-7800-8aa1-000000000000"))
        self.assertIsNone(crewchat._codex_file("*"))  # a glob is not an id
        with self.assertRaises(crewchat.ConversationUnavailable):
            self.read("codex", "0199a213-81c0-7800-8aa1-000000000000")

    def test_found_under_codex_home(self):
        elsewhere = self.home / "elsewhere"
        path = write_lines(elsewhere / "sessions" / "2026" / "10" / "20" / ("rollout-x-%s.jsonl" % UUID), [
            {"timestamp": iso("10:00"), "type": "response_item", "payload": {
                "type": "message", "role": "user", "content": [{"type": "input_text", "text": "moved home"}]}}])
        with mock.patch.dict(os.environ, {"CODEX_HOME": str(elsewhere)}):
            self.assertEqual(crewchat._codex_file(UUID), path)
            self.assertEqual([t for _, _, t in self.read("codex", UUID)], ["moved home"])

    def test_a_compressed_session_is_not_read(self):
        packed = self.rollout("rollout-2026-10-20T10-00-00-%s.jsonl.zst" % UUID)
        packed.parent.mkdir(parents=True)
        packed.write_bytes(b"\x28\xb5\x2f\xfd junk")
        self.assertIsNone(crewchat._codex_file(UUID))
        with self.assertRaises(crewchat.ConversationUnavailable):
            self.read("codex", UUID)
        with self.assertRaises(crewchat.ConversationUnavailable):  # nor handed over as a path
            self.read("codex", packed.resolve())

    def test_a_message_of_several_parts_is_one_entry_and_other_roles_are_left_out(self):
        path = write_lines(self.rollout(), [
            {"timestamp": iso("10:00"), "type": "response_item", "payload": {
                "type": "message", "role": "developer", "content": [{"type": "input_text", "text": "rules"}]}},
            {"timestamp": iso("10:01"), "type": "response_item", "payload": {
                "type": "message", "role": "user", "content": [{"type": "input_text", "text": "a"},
                                                                {"type": "input_text", "text": "b"}]}}])
        self.assertEqual(self.read("codex", path.resolve()), [(at("10:01"), "user", "a\nb")])


class Qwen(Home):
    def test_parts(self):
        path = write_lines(self.home / ".qwen" / "projects" / "x" / "chats" / "s.jsonl", [
            {"type": "system", "timestamp": iso("10:00"), "subtype": "ui_telemetry", "systemPayload": {}},
            {"type": "user", "timestamp": iso("10:01"), "message": {"role": "user", "parts": [{"text": "list files"}]}},
            {"type": "assistant", "timestamp": iso("10:02"), "message": {"role": "model", "parts": [
                {"text": "hidden thought", "thought": True},
                {"text": "Listing."},
                {"functionCall": {"id": "f1", "name": "run_shell_command", "args": {"command": "ls"}}}]}},
            {"type": "tool_result", "timestamp": iso("10:03"), "message": {"role": "user", "parts": [
                {"functionResponse": {"id": "f1", "name": "run_shell_command", "response": {"output": "a.txt"}}}]}},
            {"type": "tool_result", "timestamp": iso("10:04"), "message": {"role": "user", "parts": [
                {"functionResponse": {"id": "f2", "name": "x", "response": {"error": "boom"}}}]}}])
        self.assertEqual(self.read("qwen", path.resolve()), [
            (at("10:01"), "user", "list files"),
            (at("10:02"), "agent", "Listing."),
            (at("10:02"), "tool run_shell_command", '{"command": "ls"}'),
            (at("10:03"), "result", "a.txt"),
            (at("10:04"), "result", '{"error": "boom"}')])


class Cursor(Home):
    def transcript(self, project="proj", conversation=UUID):
        return self.home / ".cursor" / "projects" / project / "agent-transcripts" / conversation / (conversation + ".jsonl")

    def test_transcript_without_times(self):
        write_lines(self.transcript(), [
            {"role": "user", "message": {"content": [{"type": "text", "text": "add a test"}]}},
            {"role": "assistant", "message": {"content": [
                {"type": "text", "text": "Adding."},
                {"type": "tool_use", "name": "Write", "input": {"path": "t.py"}}]}},
            {"role": "assistant", "message": {"content": [{"type": "text", "text": "Done."}]}},
            {"role": "system", "message": {"content": [{"type": "text", "text": "x"}]}}])
        want = [(None, "user", "add a test"), (None, "agent", "Adding."), (None, "tool Write", '{"path": "t.py"}'),
                (None, "agent", "Done.")]
        self.assertEqual(self.read("cursor", UUID), want)  # by conversation id
        self.assertEqual(self.read("cursor", self.transcript().resolve()), want)  # by path
        self.assertEqual(self.read("cursor", UUID, at("10:00")), want)  # no times: nothing to cut at
        self.assertEqual(crewchat._cursor_file(UUID), self.transcript())
        self.assertIsNone(crewchat._cursor_file("0199a213-81c0-7800-8aa1-000000000000"))
        self.assertIsNone(crewchat._cursor_file("../../x"))

    def test_a_conversation_of_another_project_is_found_too(self):
        write_lines(self.transcript("a-project"), [{"role": "user", "message": {"content": [{"type": "text", "text": "x"}]}}])
        self.assertEqual(crewchat._cursor_file(UUID), self.transcript("a-project"))


class Gemini(Home):
    def path(self, name="session-1.jsonl"):
        return self.home / ".gemini" / "tmp" / "hash" / "chats" / name

    def lines(self):
        text = lambda t: [{"text": t}]  # noqa: E731
        return [
            {"sessionId": "s", "projectHash": "h", "startTime": iso("10:00"), "kind": "main"},
            {"id": "m1", "timestamp": iso("10:01"), "type": "user", "content": text("run ls")},
            {"id": "m2", "timestamp": iso("10:02"), "type": "gemini", "content": "Running it.",
             "thoughts": [{"subject": "Plan", "description": "hidden"}],
             "toolCalls": [{"id": "c1", "name": "run_shell", "args": {"command": "ls"}, "status": "running",
                            "timestamp": iso("10:03")}]},
            {"$patch": {"id": "m2", "toolCalls": [{"id": "c1", "status": "success", "result": [
                {"functionResponse": {"id": "c1", "name": "run_shell", "response": {"output": "a\nb"}}}]}]}},
            {"$patch": {"id": "m1", "content": text("run ls -la")}},
            {"id": "m3", "timestamp": iso("10:05"), "type": "user", "content": text("and more")},
            {"id": "m4", "timestamp": iso("10:06"), "type": "gemini", "content": "Sure."},
            {"id": "m5", "timestamp": iso("10:07"), "type": "info", "content": "Update available"},
            {"$set": {"lastUpdated": iso("10:07")}},
            {"$rewindTo": "m4"}]

    WANT = [(at("10:01"), "user", "run ls -la"), (at("10:02"), "agent", "Running it."),
            (at("10:03"), "tool run_shell", '{"command": "ls"}'), (at("10:03"), "result", "a\nb"),
            (at("10:05"), "user", "and more")]

    def test_patches_replayed(self):
        path = write_lines(self.path(), self.lines())
        self.assertEqual(self.read("gemini", path.resolve()), self.WANT)
        self.assertEqual(self.read("gemini", path.resolve(), at("10:04")), self.WANT[-1:])  # since cuts at the task

    def test_the_older_json_file_reads_the_same(self):
        lines = self.lines()
        messages = [lines[1], dict(lines[2], toolCalls=[dict(lines[2]["toolCalls"][0], status="success", result=[
            {"functionResponse": {"id": "c1", "name": "run_shell", "response": {"output": "a\nb"}}}])]),
            dict(lines[5])]
        messages[0] = dict(messages[0], content=[{"text": "run ls -la"}])
        path = self.path("session-1.json")
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps({"sessionId": "s", "messages": messages}), encoding="utf-8")
        self.assertEqual(self.read("gemini", path.resolve()), self.WANT)
        path.write_text("{not json", encoding="utf-8")
        with self.assertRaises(crewchat.ConversationUnavailable):
            self.read("gemini", path.resolve())

    def test_a_json_file_is_accepted_for_gemini_only(self):
        path = self.path("session-1.json")
        path.parent.mkdir(parents=True)
        path.write_text("{}", encoding="utf-8")
        self.assertEqual(crewchat.conversation_place("gemini", {"path": str(path)}, self.home), path.resolve())
        for tool, folder in (("claude", ".claude/projects"), ("codex", ".codex/sessions"), ("qwen", ".qwen/projects"),
                             ("cursor", ".cursor/projects")):
            other = self.home / folder / "x.json"
            other.parent.mkdir(parents=True, exist_ok=True)
            other.write_text("{}", encoding="utf-8")
            self.assertIsNone(crewchat.conversation_place(tool, {"path": str(other)}, self.home), tool)
        txt = self.path("notes.txt")
        txt.write_text("x", encoding="utf-8")
        self.assertIsNone(crewchat.conversation_place("gemini", {"path": str(txt)}, self.home))

    def test_only_the_end_of_a_huge_file_is_read(self):
        path = write_lines(self.path(), [{"id": "m0", "timestamp": iso("09:00"), "type": "user", "content": "old"}]
                           + [{"id": "p%d" % i, "timestamp": iso("09:30"), "type": "user", "content": "pad " + "y" * 100}
                              for i in range(50)]
                           + [{"id": "m9", "timestamp": iso("11:00"), "type": "user", "content": "recent"},
                              {"$patch": {"id": "m0", "content": "patched"}}])
        with mock.patch.object(crewchat, "MAX_CONVERSATION_SCAN", 2000):
            got = self.read("gemini", path.resolve())
        self.assertEqual(got[-1], (at("11:00"), "user", "recent"))
        self.assertLess(len(got), 20)  # the start of the file was not read, and no half line was taken for a record
        self.assertNotIn("old", [t for _, _, t in got])

    def replay(self, *records, base=None):
        """The (who, text) of the messages left after the records are replayed on three messages m1, m2, m3."""
        base = base or [{"id": "m%d" % i, "timestamp": iso("10:0%d" % i), "type": "user", "content": "text %d" % i}
                        for i in (1, 2, 3)]
        path = write_lines(self.path("replay.jsonl"), base + list(records))
        return [t for _, _, t in self.read("gemini", path.resolve())]

    def test_a_rewind_drops_the_named_message_and_what_follows(self):
        self.assertEqual(self.replay({"$rewindTo": "m2"}), ["text 1"])
        self.assertEqual(self.replay({"$rewindTo": "m1"}), [])

    def test_a_rewind_to_an_unknown_id_clears_everything(self):
        self.assertEqual(self.replay({"$rewindTo": "nope"}), [])
        self.assertEqual(self.replay({"$rewindTo": "nope"}, {"id": "m9", "type": "user", "content": "after"}), ["after"])

    def test_a_repeated_id_replaces_the_message_in_its_place(self):
        self.assertEqual(self.replay({"id": "m2", "type": "user", "content": "again"}), ["text 1", "again", "text 3"])

    def test_updates_patch_several_messages(self):
        self.assertEqual(self.replay({"$patch": {"updates": [{"id": "m1", "content": "one"}, {"id": "m3", "content": "three"},
                                                              {"id": "zzz", "content": "x"}, {"id": "m2", "content": None}]}}),
                         ["one", "text 2", "three"])

    def test_remove_ids_delete_messages(self):
        self.assertEqual(self.replay({"$patch": {"removeIds": ["m1", "m3", "zzz"]}}), ["text 2"])

    def test_order_ids_put_the_listed_messages_last_in_the_listed_order(self):
        self.assertEqual(self.replay({"$patch": {"orderIds": ["m1", "zzz", "m2"]}}), ["text 3", "text 1", "text 2"])

    def test_set_with_messages_replaces_them_all(self):
        self.assertEqual(self.replay({"$set": {"lastUpdated": iso("11:00")}}), ["text 1", "text 2", "text 3"])  # metadata
        self.assertEqual(self.replay({"$set": {"messages": [{"id": "n1", "type": "gemini", "content": "new"},
                                                            {"id": "n2", "type": "user", "content": "newer"}]}}),
                         ["new", "newer"])
        self.assertEqual(self.replay({"messages": [{"id": "n1", "type": "user", "content": "appended"}]}),
                         ["text 1", "text 2", "text 3", "appended"])  # the partial-metadata form

    def test_a_tool_result_arrives_by_patch(self):
        call = {"id": "c1", "name": "run_shell", "args": {"command": "ls"}, "status": "running"}
        base = [{"id": "g1", "timestamp": iso("10:01"), "type": "gemini", "content": "", "toolCalls": [call]},
                {"id": "u1", "timestamp": iso("10:02"), "type": "user", "content": "x", "toolCalls": [dict(call)]}]
        result = [{"functionResponse": {"id": "c1", "response": {"output": "done"}}}]
        path = write_lines(self.path("patched.jsonl"), base + [
            {"$patch": {"id": "g1", "toolCalls": [{"id": "c1", "result": result, "status": "success"}, {"id": "zz", "result": "x"}]}},
            {"$patch": {"id": "u1", "toolCalls": [{"id": "c1", "result": result}]}}])  # only on a gemini message
        got = self.read("gemini", path.resolve())
        self.assertEqual([(who, text) for _, who, text in got],
                         [("tool run_shell", '{"command": "ls"}'), ("result", "done"), ("user", "x")])

    def test_a_link_out_of_the_folder_is_not_read(self):
        secret = self.home / "secret.json"
        secret.write_text(json.dumps({"messages": [{"id": "m", "type": "user", "content": "key"}]}), encoding="utf-8")
        link = self.path("link.json")
        link.parent.mkdir(parents=True)
        link.symlink_to(secret)
        with self.assertRaises(crewchat.ConversationUnavailable):
            self.read("gemini", link)

    def test_a_record_of_odd_shape_is_skipped(self):
        path = write_lines(self.path(), [
            {"id": "m1", "timestamp": iso("10:01"), "type": "user", "content": 5},
            {"id": "m2", "timestamp": iso("10:02"), "type": "gemini", "content": "ok", "toolCalls": "x"},
            {"$patch": "nonsense"}, {"$patch": {"id": "zzz", "content": "x"}}, {"$rewindTo": 7}, {"$set": 5},
            {"id": "m3", "timestamp": iso("10:03"), "type": "gemini", "toolCalls": [5, {"name": "t", "args": None}]}])
        self.assertEqual(self.read("gemini", path.resolve()), [
            (at("10:02"), "agent", "ok"), (at("10:03"), "tool t", "")])


class OpenCode(Home):
    SES = "ses_1a2b3c4d5e6f"

    def db_path(self):
        return self.home / ".local" / "share" / "opencode" / "opencode.db"

    def make(self, path=None, session=SES):
        path = path or self.db_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        db = sqlite3.connect(str(path))
        db.executescript("""
            CREATE TABLE session (id text PRIMARY KEY, title text);
            CREATE TABLE message (id text PRIMARY KEY, session_id text NOT NULL, time_created integer NOT NULL,
                                  time_updated integer NOT NULL, data text NOT NULL);
            CREATE TABLE part (id text PRIMARY KEY, message_id text NOT NULL, session_id text NOT NULL,
                               time_created integer NOT NULL, time_updated integer NOT NULL, data text NOT NULL);""")
        db.execute("INSERT INTO session VALUES (?, 't')", (session,))
        db.commit()
        return db

    def add(self, db, mid, role, when, parts, session=SES):
        ms = int(at(when) * 1000)
        db.execute("INSERT INTO message VALUES (?, ?, ?, ?, ?)", (mid, session, ms, ms, json.dumps({"role": role})))
        for i, part in enumerate(parts):
            db.execute("INSERT INTO part VALUES (?, ?, ?, ?, ?, ?)",
                       ("%s_p%d" % (mid, i), mid, session, ms, ms, json.dumps(part)))
        db.commit()

    def fill(self, db):
        self.add(db, "msg_2", "assistant", "10:02", [
            {"type": "reasoning", "text": "hidden"}, {"type": "text", "text": "Listing."},
            {"type": "tool", "callID": "x", "tool": "bash",
             "state": {"status": "completed", "input": {"command": "ls"}, "output": "a\nb"}}])
        self.add(db, "msg_1", "user", "10:01", [{"type": "text", "text": "list files"}])  # inserted out of order
        self.add(db, "msg_3", "assistant", "10:03", [
            {"type": "tool", "tool": "read", "state": {"status": "running", "input": {"path": "x"}}},
            {"type": "tool", "tool": "edit", "state": {"status": "error", "input": {"p": 1}, "error": "no such file"}}])
        self.add(db, "msg_4", "user", "10:04", [{"type": "text", "text": "other session"}], session="ses_other0000000")

    WANT = [(at("10:01"), "user", "list files"), (at("10:02"), "agent", "Listing."),
            (at("10:02"), "tool bash", '{"command": "ls"}'), (at("10:02"), "result", "a\nb"),
            (at("10:03"), "tool read", '{"path": "x"}'), (at("10:03"), "tool edit", '{"p": 1}'),
            (at("10:03"), "result", "no such file")]

    def test_messages_and_tool_parts(self):
        db = self.make()
        self.fill(db)
        db.close()
        self.assertEqual(self.read("opencode", self.SES), self.WANT)
        self.assertEqual(self.read("opencode", self.SES, at("10:02")), self.WANT[1:])  # since cuts at the task
        cut = self.read("opencode", self.SES, budget=10)  # the newest are kept, their text cut to the budget
        self.assertEqual(cut[-1], (at("10:03"), "result", "no such fi"))
        self.assertLess(len(cut), len(self.WANT))

    def test_the_database_named_by_the_variable_and_a_path_with_odd_characters(self):
        odd = self.home / "my data #1?" / "oc.db"
        db = self.make(odd)
        self.fill(db)
        db.close()
        with mock.patch.dict(os.environ, {"OPENCODE_DB": str(odd)}):
            self.assertEqual(self.read("opencode", self.SES), self.WANT)

    def test_the_database_is_not_changed(self):
        db = self.make()
        self.fill(db)
        db.close()
        before = self.db_path().read_bytes()
        self.read("opencode", self.SES)
        self.assertEqual(self.db_path().read_bytes(), before)

    def test_a_locked_database_is_not_available(self):
        db = self.make()
        self.fill(db)
        db.close()
        holder = sqlite3.connect(str(self.db_path()), isolation_level=None)
        self.addCleanup(holder.close)
        holder.execute("BEGIN EXCLUSIVE")
        started = time.monotonic()
        with self.assertRaises(crewchat.ConversationUnavailable) as caught:
            self.read("opencode", self.SES)
        self.assertLess(time.monotonic() - started, 3)
        self.assertIn("database", caught.exception.reason)

    def test_a_missing_or_wrong_database_is_not_available(self):
        with self.assertRaises(crewchat.ConversationUnavailable) as caught:
            self.read("opencode", self.SES)
        self.assertIn("database", caught.exception.reason)
        self.db_path().parent.mkdir(parents=True)
        self.db_path().write_text("this is not a database", encoding="utf-8")
        with self.assertRaises(crewchat.ConversationUnavailable):
            self.read("opencode", self.SES)
        self.db_path().unlink()
        self.make().close()  # a good database: what is refused is the place, not the database
        self.assertEqual(self.read("opencode", self.SES), [])
        for place in (None, 5, "0199a213-81c0-7800-8aa1-bbab2a035a53", "ses_../../x", self.db_path()):
            with self.assertRaises(crewchat.ConversationUnavailable, msg=place):
                self.read("opencode", place)

    def test_an_unknown_session_gives_nothing(self):
        self.make().close()
        self.assertEqual(self.read("opencode", "ses_0000000000aa"), [])

    def test_seconds_are_taken_as_they_are(self):
        db = self.make()
        db.execute("INSERT INTO message VALUES ('m', ?, 1760954460, 1760954460, ?)", (self.SES, json.dumps({"role": "user"})))
        db.execute("INSERT INTO part VALUES ('p', 'm', ?, 1760954460, 1760954460, ?)",
                   (self.SES, json.dumps({"type": "text", "text": "hi"})))
        db.commit()
        db.close()
        self.assertEqual(self.read("opencode", self.SES), [(1760954460, "user", "hi")])


class Counting:
    """Stands in for open() and counts the reads made on the files opened through it."""

    def __init__(self):
        self.reads = self.bytes = 0
        self.real = open

    def open(self, *args, **kw):
        counter, f = self, self.real(*args, **kw)

        class Proxy:
            def __enter__(self):
                return self

            def __exit__(self, *exc):
                f.close()

            def __getattr__(self, name):
                return getattr(f, name)

            def read(self, *a):
                data = f.read(*a)
                counter.reads += 1
                counter.bytes += len(data)
                return data

        return Proxy()


class Robustness(Home):
    def path(self):
        return self.home / ".claude" / "projects" / "x" / "s.jsonl"

    def test_a_half_written_line_and_bad_utf8_are_skipped(self):
        first = json.dumps({"type": "user", "timestamp": iso("10:00"), "message": {"role": "user", "content": "one"}})
        last = json.dumps({"type": "assistant", "timestamp": iso("10:02"), "message": {"role": "assistant", "content": "two"}})
        path = self.path()
        path.parent.mkdir(parents=True)
        path.write_bytes(first.encode() + b"\n\xff\xfe\n" + b"[1, 2]\n\n" + b"not json\n" + last.encode() + b"\n"
                         + b'{"type": "user", "mess')
        self.assertEqual(self.read("claude", path.resolve()), [(at("10:00"), "user", "one"), (at("10:02"), "agent", "two")])

    def test_odd_records_do_not_raise(self):
        odd = [{"type": "user", "message": "text"}, {"type": "user", "message": {"content": 5}},
               {"type": "assistant", "message": {"content": [None, 5, "x", {"type": "tool_use"}, {"type": "text"}]}},
               {"type": "user", "message": {"content": [{"type": "tool_result", "content": None}]}},
               {"message": {"content": "no type"}}, {"type": 5}]
        path = write_lines(self.path(), odd)
        self.read("claude", path.resolve())
        for tool, rec in (("codex", {"type": "response_item", "payload": "x"}), ("codex", {"payload": {"type": 5}}),
                          ("qwen", {"type": "user", "message": {"parts": ["x", 5, {"functionCall": 5}]}}),
                          ("cursor", {"role": "user", "message": "text"}), ("cursor", {"role": "assistant"}),
                          ("codex", {"type": "response_item", "payload": {"type": "message", "role": []}}),
                          ("codex", {"type": "response_item", "payload": {"type": "message", "role": {}}}),
                          ("cursor", {"role": {}, "message": {}}), ("cursor", {"role": [], "message": {}})):
            reader = crewchat.CONVERSATION_READERS[tool]
            self.assertIsInstance(reader(rec), list)

    def test_a_record_that_breaks_its_reader_is_skipped(self):
        path = write_lines(self.path(), [
            {"type": "user", "timestamp": iso("10:00"), "message": {"role": "user", "content": "kept"}},
            {"type": "user", "timestamp": iso("10:01"), "message": {"role": "user", "content": "broken"}}])
        real = crewchat._claude_entries

        def reader(record):
            if record["message"]["content"] == "broken":
                raise TypeError("unexpected shape")
            return real(record)

        with mock.patch.dict(crewchat.CONVERSATION_READERS, {"claude": reader}):
            self.assertEqual(self.read("claude", path.resolve()), [(at("10:00"), "user", "kept")])

    def test_a_huge_file_is_read_from_its_end(self):
        path = self.path()
        path.parent.mkdir(parents=True)
        pad = json.dumps({"type": "user", "timestamp": iso("08:00"), "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t", "content": "x" * 4000}]}}).encode() + b"\n"
        with open(path, "wb") as f:
            for _ in range(64 * 1024 * 1024 // len(pad)):  # about 64 MB, many lines
                f.write(pad)
            for i, minute in enumerate(("10:01", "10:02", "10:03")):
                f.write(json.dumps({"type": "assistant", "timestamp": iso(minute), "message": {
                    "role": "assistant", "content": "recent %d" % i}}).encode() + b"\n")
        self.assertGreater(path.stat().st_size, 60 * 1024 * 1024)
        for since, budget in ((at("10:00"), 4 * crewchat.EXCERPT_BYTES), (None, 20000)):
            counter = Counting()
            begun = time.time()
            with mock.patch("builtins.open", counter.open):
                got = self.read("claude", path.resolve(), since, budget=budget)
            self.assertLess(time.time() - begun, 1)
            self.assertLess(counter.reads, 20)  # a block or two from the end, not the file
            self.assertLess(counter.bytes, 2 * 1024 * 1024)  # of 64 MB
            self.assertEqual([t for _, _, t in got][-3:], ["recent 0", "recent 1", "recent 2"])
        self.assertLess(len(got), 20)  # the budget cut the padding
        self.assertEqual(len(self.read("claude", path.resolve(), at("10:00"))), 3)  # since cut it at the task

    def test_records_the_reader_does_not_know_are_scanned_only_so_far(self):
        path = write_lines(self.path(), [{"type": "user", "timestamp": iso("09:00"),
                                          "message": {"role": "user", "content": "far back"}}]
                           + [{"type": "progress", "data": "y" * 1000}] * 200)
        with mock.patch.object(crewchat, "MAX_CONVERSATION_SCAN", 50 * 1000):
            self.assertEqual(self.read("claude", path.resolve()), [])
        self.assertEqual(len(self.read("claude", path.resolve())), 1)

    def test_an_unreadable_place_says_why(self):
        missing = self.path()
        missing.parent.mkdir(parents=True)
        place = crewchat.conversation_place("claude", {"path": str(missing)}, self.home)
        with self.assertRaises(crewchat.ConversationUnavailable) as caught:
            self.read("claude", place)
        self.assertEqual(caught.exception.reason, "its conversation file is missing")
        # The reasons go into the question every agent reads: they name no file (its name is a session's id).
        outside = write_lines(self.home / "elsewhere.jsonl", [{"type": "user", "message": {"content": "x"}}])
        reasons = []
        for swap in ("link", "unreadable"):
            if missing.is_symlink() or missing.exists():
                missing.unlink()
            if swap == "link":
                missing.symlink_to(outside)
            else:
                write_lines(missing, [{"type": "user", "message": {"content": "x"}}])
                missing.chmod(0)
                self.addCleanup(missing.chmod, 0o600)
            with self.assertRaises(crewchat.ConversationUnavailable, msg=swap) as caught:
                self.read("claude", place)
            reasons.append(caught.exception.reason)
        self.assertEqual(reasons, ["its conversation file is not where claude keeps its conversations",
                                   "its conversation file cannot be read"])
        with self.assertRaises(crewchat.ConversationUnavailable) as caught:
            self.read("codex", UUID)
        reasons.append(caught.exception.reason)
        for reason in reasons:
            self.assertNotIn("s.jsonl", reason)
            self.assertNotIn(UUID, reason)
            self.assertNotIn(str(self.home), reason)
        for tool, place in (("claude", None), ("claude", UUID), ("codex", 5), ("opencode", "ses_abc12345"),
                            ("gemini", UUID), ("nope", missing)):
            with self.assertRaises(crewchat.ConversationUnavailable, msg=(tool, place)):
                self.read(tool, place)


class Labels(unittest.TestCase):
    def test_every_tool_read_is_named_from_launch_tools(self):
        # Final review of A2: the excerpt's first line names the tool from LAUNCH_TOOLS, which has all six; the
        # second table of names that stood in for it is gone.
        for tool in set(crewchat.CONVERSATION_READERS) | {"gemini", "opencode"}:
            self.assertIn(tool, crewchat.LAUNCH_TOOLS)
        self.assertFalse(hasattr(crewchat, "TOOL_LABELS"))


class BoilDown(unittest.TestCase):
    """boil_down: the entries as the text the advisor is shown."""

    def boil(self, entries, task="12", since=None, agent="Maya", label="Claude Code"):
        return crewchat.boil_down(entries, agent, label, task, at("14:02") if since is None and task else since)

    def test_header_and_lines(self):
        entries = [(at("14:03"), "user", "Fix the login crash"), (at("14:04"), "agent", "On it.\nLooking at the trace.")]
        text = self.boil(entries)
        lines = text.split("\n")
        self.assertEqual(lines[0], "Maya's conversation (Claude Code) since taking task #12 at 14:02 UTC, "
                                   "as crewchat boiled it down for the advisor.")
        self.assertEqual(lines[1], "Tool results are cut; secrets are left out.")
        self.assertIn("[14:03] user: Fix the login crash", lines)
        self.assertIn("[14:04] Maya: On it.", lines)
        self.assertIn("Looking at the trace.", text)
        self.assertNotIn("left out to keep", text)
        plain = crewchat.boil_down(entries, "Maya", "Claude Code", None, None)
        self.assertEqual(plain.split("\n")[0], "Maya's conversation (Claude Code), its last 40 KB, "
                                               "as crewchat boiled it down for the advisor.")

    def test_without_any_times_the_excerpt_is_the_last_40_kb_even_with_a_task(self):
        # Cursor's transcript has no times: it cannot be cut at the task, so the header must not say it starts there.
        text = crewchat.boil_down([(None, "user", "hello"), (None, "agent", "hi")], "Maya", "Cursor", "12", 1760000000.0)
        self.assertEqual(text.split("\n")[0], "Maya's conversation (Cursor), its last 40 KB, "
                                              "as crewchat boiled it down for the advisor.")
        self.assertNotIn("since taking", text)
        timed = crewchat.boil_down([(None, "user", "hello"), (at("14:03"), "agent", "hi")], "Maya", "Codex", "12", at("14:02"))
        self.assertIn("since taking task #12 at 14:02 UTC", timed)  # one timed entry: it was cut by time

    def test_an_entry_without_a_time_has_none_shown(self):
        text = self.boil([(None, "user", "hello"), (at("09:05"), "agent", "hi")], task=None)
        self.assertIn("\nuser: hello", text)
        self.assertNotIn("[None]", text)
        self.assertIn("[09:05] Maya: hi", text)

    def test_a_tool_call_is_shortened(self):
        def call(name, arguments):
            return crewchat.boil_down([(at("14:04"), "tool " + name, json.dumps(arguments, sort_keys=True))],
                                      "Maya", "Claude Code", None, None)
        self.assertIn("[14:04] tool Bash: ./gradlew test\n", call("Bash", {"command": "./gradlew test", "timeout": 5}) + "\n")
        self.assertIn("tool Read: app/Login.kt", call("Read", {"file_path": "app/Login.kt", "limit": 10}))
        self.assertIn("tool Glob: src/**", call("Glob", {"path": "src/**"}))
        self.assertIn("tool Fetch: https://x.dev/a", call("Fetch", {"url": "https://x.dev/a", "prompt": "p"}))
        other = call("Task", {"description": "d" * 300})
        self.assertIn('{"description": "' + "d" * 183, other)
        self.assertNotIn("d" * 184, other)
        self.assertNotIn("d" * 201, call("Bash", {"command": "d" * 300}))
        self.assertIn("tool Odd: just words", crewchat.boil_down([(None, "tool Odd", "just words")], "Maya", "Claude Code", None, None))

    def test_a_long_result_keeps_its_ends(self):
        body = "\n".join("line %d" % i for i in range(1, 401))
        text = self.boil([(at("14:05"), "result", body)])
        self.assertIn("result (400 lines, 380 cut):", text)
        for n in list(range(1, 11)) + list(range(391, 401)):
            self.assertIn("line %d\n" % n, text + "\n")
        for n in (11, 200, 390):
            self.assertNotIn("line %d\n" % n, text + "\n")
        entry = text.split("\n\n", 1)[1]
        self.assertLessEqual(len(entry.encode("utf-8")), 2048)
        wide = self.boil([(None, "result", "\n".join("w" * 500 + str(i) for i in range(400)))], task=None)
        self.assertLessEqual(len(wide.split("\n\n", 1)[1].encode("utf-8")), 2048)
        self.assertIn("(400 lines, 380 cut)", wide)
        short = self.boil([(None, "result", "a\nb")], task=None)
        self.assertIn("result (2 lines, 0 cut):", short)

    def test_the_limit_drops_the_oldest(self):
        entries = [(at("10:00") + i, "agent", "%04d " % i + "x" * 1019) for i in range(500)]
        text = self.boil(entries, task=None)
        self.assertLessEqual(len(text.encode("utf-8")), crewchat.EXCERPT_BYTES)
        self.assertIn("0499 ", text)
        self.assertNotIn("0000 ", text)
        self.assertEqual(text.split("\n")[3], "(older steps left out to keep this under 40 KB)")

    def test_the_limit_counts_bytes_and_never_cuts_a_character(self):
        entries = [(None, "agent", "\u00e9\u4e2d" * 3000) for _ in range(30)]
        text = self.boil(entries, task=None)
        self.assertLessEqual(len(text.encode("utf-8")), crewchat.EXCERPT_BYTES)
        self.assertNotIn("\ufffd", text)
        huge = self.boil([(None, "user", "\u4e2d" * 100000)], task=None)  # one entry over the limit alone
        self.assertLessEqual(len(huge.encode("utf-8")), crewchat.EXCERPT_BYTES)
        self.assertNotIn("\ufffd", huge)
        self.assertIn("user: \u4e2d", huge)

    def test_secrets_are_left_out(self):
        secrets_ = {
            "key": "-----BEGIN RSA PRIVATE KEY-----\nMIIEabc\nxyz123\n-----END RSA PRIVATE KEY-----",
            "sk": "sk-" + "a1B2c3D4" * 4,
            "ghp": "ghp_" + "A" * 24,
            "pat": "github_pat_" + "b" * 24,
            "aws": "AKIA" + "0123456789ABCDEF",
            "xox": "xoxb-1234567890-abcdef",
            "bearer": "Bearer abcdefghijklmnop1234.-_~",
        }
        for name, secret in secrets_.items():
            for who, text in (("result", "before\n%s\nafter" % secret), ("agent", "token %s here" % secret)):
                got = self.boil([(None, who, text)], task=None)
                self.assertIn("[secret left out]", got, name)
                self.assertNotIn(secret[:20], got, name)
                self.assertNotIn("MIIEabc", got, name)
                self.assertNotIn("xyz123", got, name)
                self.assertTrue("before" in got and "after" in got or "token" in got and "here" in got, name)
        call = self.boil([(None, "tool Bash", json.dumps({"command": "curl -H 'Authorization: Bearer %s' x" % ("q" * 30)}))], task=None)
        self.assertIn("[secret left out]", call)
        self.assertNotIn("q" * 16, call)
        cut = self.boil([(None, "result", "-----BEGIN PRIVATE KEY-----\nMIIE" + "A" * 100)], task=None)  # no end marker
        self.assertIn("[secret left out]", cut)
        self.assertNotIn("MIIE", cut)
        self.assertEqual(crewchat.redact("sk-short and Bearer short"), "sk-short and Bearer short")

    def test_words_that_end_in_sk_are_not_secrets_and_bearer_has_any_case(self):
        # Final review of A2 (R9): sk- only at the start of a word; Bearer as a header writes it, in any case.
        for text in ("docs/task-management-service-handler.md", "risk-assessment-framework-v2",
                     "see disk-usage-report-for-monday", "ask-the-advisor-before-you-merge"):
            self.assertEqual(crewchat.redact(text), text)
        self.assertEqual(crewchat.redact("key=sk-abcdefghijklmnopqrstuv end"), "key=[secret left out] end")
        self.assertEqual(crewchat.redact("(sk-abcdefghijklmnopqrstuv)"), "([secret left out])")
        for header in ("authorization: bearer abcdefghijklmnop1234", "Authorization: BEARER abcdefghijklmnop1234"):
            self.assertEqual(crewchat.redact(header), header.split(": ")[0] + ": [secret left out]")
        self.assertEqual(crewchat.redact("the bearer of bad news"), "the bearer of bad news")

    def test_cursor_user_text_is_what_is_inside_user_query(self):
        text = self.boil([(None, "user", "<user_query>\nFix the crash\n</user_query>")], task=None)
        self.assertIn("user: Fix the crash", text)
        self.assertNotIn("user_query", text)

    def test_codex_injected_context_is_left_out(self):
        entries = [(None, "user", "<environment_context>\n  <cwd>/x</cwd>\n</environment_context>"),
                   (None, "user", "<user_instructions>be nice</user_instructions>"),
                   (None, "user", "# AGENTS.md instructions for /x\n\n<INSTRUCTIONS>…"),
                   (None, "agent", "# AGENTS.md instructions are read"),
                   (None, "user", "Please fix it")]
        text = self.boil(entries, task=None)
        for gone in ("environment_context", "user_instructions", "be nice", "<INSTRUCTIONS>"):
            self.assertNotIn(gone, text)
        self.assertIn("user: Please fix it", text)
        self.assertIn("Maya: # AGENTS.md instructions are read", text)  # only the user's entries are judged


if __name__ == "__main__":
    unittest.main()
