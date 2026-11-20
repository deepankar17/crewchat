"""Run the end-to-end tests as CI does (the e2e workflow): `python3 e2e/ci.py`.

The same as `python3 -m unittest discover -s e2e -v`, except that a run which skipped the chat-page or installer tests
because Chrome, websocket-client or uv was missing fails: it would pass while testing much less than it says. So does
one that skipped the upgrade tests because the old release's tag is not in the clone (git fetch --tags), unless
CREWCHAT_E2E_NO_OLD_TAGS=1 says that is expected: the public repo, whose history starts at 1.0.0.
CREWCHAT_CHROME names the Chrome to use (test_browser.py).
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
# The skip reasons that mean a tool is missing, not that a test does not apply on this system.
NEEDS = ("needs Google Chrome", "needs Chrome", "needs uv")
OLD_TAG = "the old release "


def missing(skipped, old_tags=True):
    """The distinct skip reasons, from unittest's (test, reason) list, that mean a needed tool (or, with old_tags, an
    old release's tag) is missing."""
    needs = NEEDS + ((OLD_TAG,) if old_tags else ())
    return sorted({reason for _, reason in skipped if reason.startswith(needs)})


def main():
    suite = unittest.defaultTestLoader.discover(HERE, top_level_dir=HERE)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    lacking = missing(result.skipped, old_tags=os.environ.get("CREWCHAT_E2E_NO_OLD_TAGS") != "1")
    for reason in lacking:
        print("e2e: tests were skipped because they %s: install it, or the run checks less than it says" % reason)
    return 0 if result.wasSuccessful() and not lacking else 1


if __name__ == "__main__":
    sys.exit(main())
