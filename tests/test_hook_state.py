"""Where the hooks keep a session's state and steps (round-10 review, Security): the system's temporary folder is
shared by every user of the machine, so its crewchat-hooks folder is used only while it is this user's own."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from crewchat_fixture import (  # noqa: E402,F401  (the shared server fixture)
    Path, TMP, crewchat, mock, os, tempfile, unittest,
)


@unittest.skipUnless(hasattr(os, "getuid"), "POSIX owners and modes")
class HookStateFolder(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="hookstate-tmp-", dir=TMP))
        self.user = Path(tempfile.mkdtemp(prefix="hookstate-user-", dir=TMP))
        for patch in (mock.patch.object(crewchat.tempfile, "gettempdir", lambda: str(self.tmp)),
                      mock.patch.object(crewchat.Path, "home", classmethod(lambda cls: self.user))):
            patch.start()
            self.addCleanup(patch.stop)

    def test_its_own_private_folder_is_used(self):
        path = crewchat.hook_state_file("claude", "s1")
        self.assertEqual(path.parent, self.tmp / "crewchat-hooks")
        self.assertEqual(os.stat(path.parent).st_mode & 0o777, 0o700)

    def test_its_own_folder_left_open_is_made_private(self):
        (self.tmp / "crewchat-hooks").mkdir(mode=0o755)
        os.chmod(self.tmp / "crewchat-hooks", 0o755)
        self.assertEqual(crewchat.hook_state_file("claude", "s1").parent, self.tmp / "crewchat-hooks")
        self.assertEqual(os.stat(self.tmp / "crewchat-hooks").st_mode & 0o777, 0o700)

    def test_another_user_s_folder_is_not_used(self):
        (self.tmp / "crewchat-hooks").mkdir(mode=0o700)
        real = os.lstat
        other = lambda p, *a, **k: os.stat_result((0o40700, 0, 0, 0, os.getuid() + 1) + tuple(real(p))[5:]) \
            if str(p) == str(self.tmp / "crewchat-hooks") else real(p, *a, **k)
        with mock.patch.object(crewchat.os, "lstat", other):
            self.assertEqual(crewchat.hook_state_file("claude", "s1").parent, self.user / ".cache" / "crewchat-hooks")

    def test_a_link_in_its_place_is_not_followed(self):
        elsewhere = Path(tempfile.mkdtemp(prefix="hookstate-else-", dir=TMP))
        os.chmod(elsewhere, 0o700)
        (self.tmp / "crewchat-hooks").symlink_to(elsewhere)
        self.assertEqual(crewchat.hook_state_file("claude", "s1").parent, self.user / ".cache" / "crewchat-hooks")


if __name__ == "__main__":
    unittest.main()
