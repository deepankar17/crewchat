"""The end-to-end suite runs everywhere: a test class that starts Chrome skips where Chrome or websocket-client is
missing, rather than erroring (a review found one that had lost its guard)."""
import ast
import unittest
from pathlib import Path

E2E = Path(__file__).resolve().parents[1] / "e2e"


class Guards(unittest.TestCase):
    def test_every_class_that_starts_chrome_can_skip(self):
        tree = ast.parse((E2E / "test_browser.py").read_text(encoding="utf-8"))
        bases = {}
        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                bases[node.name] = node
        def guarded(node):
            if any(isinstance(d, ast.Call) and getattr(d.func, "attr", "") == "skipUnless" for d in node.decorator_list):
                return True
            return any(isinstance(b, ast.Name) and b.id in bases and guarded(bases[b.id]) for b in node.bases)
        for name, node in bases.items():
            starts = any(isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "Chrome"
                         for n in ast.walk(node))
            if starts and name != "Signed":  # the base the guarded classes share: never run on its own
                self.assertTrue(guarded(node), "%s starts Chrome without a skip guard" % name)


if __name__ == "__main__":
    unittest.main()
