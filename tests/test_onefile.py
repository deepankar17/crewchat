"""The built crewchat.py is the join of crewchat_src/, and that built file still runs copied alone."""
import ast
import importlib.util
import inspect
import re
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def _builder():
    path = ROOT / "tools" / "build" / "onefile.py"
    spec = importlib.util.spec_from_file_location("crewchat_onefile", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class OneFile(unittest.TestCase):
    def test_the_built_file_matches_the_sources(self):
        # A public checkout has the built file and not the sources. This repo has both, and they must match.
        if not (ROOT / "tools" / "publish" / "publish.py").is_file() and not (ROOT / "crewchat_src").is_dir():
            self.skipTest("public checkout ships the built file only")
        before = (ROOT / "crewchat.py").read_text(encoding="utf-8")
        built = _builder().build(ROOT)
        self.assertEqual(before, built)
        self.assertEqual(before, (ROOT / "crewchat.py").read_text(encoding="utf-8"))
        if (ROOT / "crewchat_src" / "hub.py").is_file() and (ROOT / "crewchat_src" / "cli.py").is_file():
            self.assertFalse((ROOT / "crewchat_src" / "rest.py").exists())

    def test_the_built_file_names_no_source_directory(self):
        self.assertNotIn("crewchat_src", (ROOT / "crewchat.py").read_text(encoding="utf-8"))

    def test_a_copied_file_runs_alone(self):
        folder = Path(tempfile.mkdtemp(prefix="crewchat-alone-"))
        self.addCleanup(shutil.rmtree, folder, True)
        shutil.copy(ROOT / "crewchat.py", folder / "crewchat.py")
        self.assertEqual(list(folder.iterdir()), [folder / "crewchat.py"])
        done = subprocess.run([sys.executable, str(folder / "crewchat.py"), "--version"],
                              capture_output=True, text=True)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("crewchat ", done.stdout)
        spec = importlib.util.spec_from_file_location("crewchat_alone", folder / "crewchat.py")
        alone = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(alone)
        alone.hook_script()
        names, functions = _hook_lists(alone.hook_script)
        for name in names:
            self.assertIn(name, vars(alone), name)
        for fn in functions:
            source = inspect.getsource(getattr(alone, fn))
            self.assertIn("def %s" % fn, source)

    def test_ci_checks_the_built_file_before_the_suite(self):
        # A release job runs the same suite. It has to refuse a drifted crewchat.py too.
        text = (ROOT / ".github" / "workflows" / "test.yml").read_text(encoding="utf-8")
        jobs = re.split(r"(?m)^  [A-Za-z0-9-]+:\n", text)[1:]
        saw = False
        for job in jobs:
            if "python -m unittest discover" not in job:
                continue
            saw = True
            check = job.find("onefile.py --check")
            suite = job.find("python -m unittest discover")
            self.assertGreater(check, -1, job[:80])
            self.assertLess(check, suite)
        self.assertTrue(saw)


def _hook_lists(hook_script):
    """The names and functions hook_script copies, read from its source so the test follows the lists it ships."""
    tree = ast.parse(inspect.getsource(hook_script))
    names = functions = None
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            if not isinstance(target, ast.Name):
                continue
            if target.id == "names":
                names = ast.literal_eval(node.value)
            elif target.id == "functions":
                functions = tuple(elt.id for elt in node.value.elts)
    return names, functions
