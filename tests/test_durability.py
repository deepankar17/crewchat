"""Durability basics (review finding 9): files written in one step reach the disk before the rename, a
state.json that cannot be read is kept aside rather than overwritten, and state.json and events.sqlite say
which format wrote them."""
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import crewchat  # noqa: E402


class Durability(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="durable-"))
        crewchat.save_config({"project": "durable", "port": 9}, self.root)

    def hub(self):
        hub = crewchat.Hub(crewchat.Roster(self.root))
        self.addCleanup(hub.events.close)
        return hub

    def test_write_text_syncs_the_file_before_the_rename(self):
        order = []
        real_fsync, real_replace = os.fsync, os.replace
        with mock.patch.object(crewchat.os, "fsync", side_effect=lambda fd: (order.append("fsync"), real_fsync(fd))), \
                mock.patch.object(crewchat.os, "replace",
                                  side_effect=lambda a, b: (order.append("replace"), real_replace(a, b))):
            crewchat.write_json(self.root / "x.json", {"a": 1})
        self.assertEqual(order, ["fsync", "replace"])
        self.assertEqual(json.loads((self.root / "x.json").read_text()), {"a": 1})

    def test_a_corrupt_state_json_is_kept_aside(self):
        (self.root / "state.json").write_text('{"agents": {"maya": ', encoding="utf-8")
        with mock.patch.object(crewchat.sys, "stderr") as err:
            hub = self.hub()
        self.assertEqual(hub.agents, {})
        aside = list(self.root.glob("state.json.corrupt-*"))
        self.assertEqual(len(aside), 1)
        self.assertEqual(aside[0].read_text(encoding="utf-8"), '{"agents": {"maya": ')  # nothing lost
        self.assertFalse((self.root / "state.json").exists())
        self.assertIn("kept as state.json.corrupt-", "".join(c.args[0] for c in err.write.call_args_list))

    def test_state_json_that_is_not_an_object_is_kept_aside_too(self):
        (self.root / "state.json").write_text("[1, 2]", encoding="utf-8")
        with mock.patch.object(crewchat.sys, "stderr"):
            self.hub()
        self.assertEqual(len(list(self.root.glob("state.json.corrupt-*"))), 1)

    def test_no_state_json_is_a_new_start_with_nothing_kept_aside(self):
        self.hub()
        self.assertEqual(list(self.root.glob("state.json.corrupt-*")), [])

    def test_state_json_carries_its_format(self):
        hub = self.hub()
        with hub.lock:
            hub._save()
        self.assertEqual(json.loads((self.root / "state.json").read_text())["format"], crewchat.STATE_FORMAT)

    def test_a_newer_state_json_is_read_and_said_so(self):
        crewchat.write_json(self.root / "state.json", {"format": crewchat.STATE_FORMAT + 1, "agents": {}})
        with mock.patch.object(crewchat.sys, "stderr") as err:
            self.hub()
        self.assertIn("newer crewchat", "".join(c.args[0] for c in err.write.call_args_list))
        self.assertEqual(list(self.root.glob("state.json.corrupt-*")), [])

    def test_events_sqlite_carries_its_format(self):
        hub = self.hub()
        hub.events.add_edit("maya", "a.py")
        hub.events.flush()
        db = sqlite3.connect(str(self.root / crewchat.EVENTS_FILE))
        try:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], crewchat.EVENTS_FORMAT)
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
