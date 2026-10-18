"""crewchat's code map (roadmap C1): the files, definitions and imports of a project's code, so the chat can
tell which tasks and agents touch code that depends on each other. Standard library only; the server loads it
only for projects that keep edit events (C0), the way it loads cloud sync.

One SQLite file per repository and branch, in the project's crewchat folder: maps/<key>.sqlite. It is derived
data (docs/storage.md): delete it and the server builds it again from the code.

  nodes(id, kind, path, name, line, lang)   a file, or a definition in one (class, function, component...)
  edges(src, dst, kind, certain)            imports, calls, uses (a template using a component), injects
  refs(path, kind, target, certain)         what each file points at, kept to link again when files change
  files(path, mtime, size)                  what was read, to read again only what changed
  signatures(path, name, signature)         each Python function's parameters, for interface-change notices (C3)
  meta(key, value)                          the repository, branch and commit the map was built from

Python is read with the ast module. Java, Kotlin, React (JS, JSX, TS, TSX) and Angular are read with regular
expressions. A link that resolves to a file of the repository is certain; a guess (a bare name in the same
package, a class name defined in more than one file) is stored with certain = 0 and only ever used as a hint.
"""
import ast
import hashlib
import json
import os
import re
import sqlite3
import subprocess
import threading
import time
from pathlib import Path

MAX_FILES = 20000  # read at most this many source files of one repository
MAX_FILE_BYTES = 1024 * 1024  # bigger source files are skipped (generated, minified)
MAX_GUESSES = 3  # a name defined in more files than this is not linked at all
CERTAIN, GUESS = 2, 1  # how two areas are linked (CodeMap.link)
SKIP_DIRS = {".git", "node_modules", "build", "dist", "out", "target", ".gradle", ".idea", "__pycache__", ".venv",
             "venv", ".tox", ".mypy_cache", ".pytest_cache", "coverage", ".next", ".angular", "Pods", "obj",
             "generated", ".dart_tool", "vendor", "site-packages"}
LANGS = {".py": "python", ".java": "java", ".kt": "kotlin", ".kts": "kotlin", ".js": "react", ".jsx": "react",
         ".mjs": "react", ".cjs": "react", ".ts": "react", ".tsx": "react", ".html": "html"}
JS_EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs")
PY_ROOTS = ("", "src/", "lib/")  # where a Python import may start, besides the importing file's own folder
JVM_ROOTS = ("src/main/java/", "src/main/kotlin/", "src/test/java/", "src/test/kotlin/", "src/")
SCHEMA = """
CREATE TABLE IF NOT EXISTS nodes (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, path TEXT NOT NULL, name TEXT NOT NULL,
                                  line INTEGER NOT NULL DEFAULT 0, lang TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS nodes_path ON nodes (path);
CREATE TABLE IF NOT EXISTS edges (src INTEGER NOT NULL, dst INTEGER NOT NULL, kind TEXT NOT NULL,
                                  certain INTEGER NOT NULL DEFAULT 1);
CREATE INDEX IF NOT EXISTS edges_src ON edges (src);
CREATE INDEX IF NOT EXISTS edges_dst ON edges (dst);
CREATE TABLE IF NOT EXISTS refs (path TEXT NOT NULL, kind TEXT NOT NULL, target TEXT NOT NULL,
                                 certain INTEGER NOT NULL DEFAULT 1);
CREATE INDEX IF NOT EXISTS refs_path ON refs (path);
CREATE TABLE IF NOT EXISTS files (path TEXT PRIMARY KEY, mtime REAL NOT NULL, size INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS signatures (path TEXT NOT NULL, name TEXT NOT NULL, signature TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS signatures_path ON signatures (path);
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""
MAP_VERSION = "2"  # what the tables hold; a map of another version is built again


# --------------------------------------------------------------------------------------------
# Which repository and branch a folder is
# --------------------------------------------------------------------------------------------
def git(folder, *args):
    try:
        out = subprocess.run(["git", "-C", str(folder)] + list(args), capture_output=True, text=True, timeout=20,
                             stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def clean_remote(url):
    """A remote's address as one name for the repository: git@github.com:a/b.git and https://github.com/a/b
    are the same repository."""
    url = re.sub(r"^[a-z][a-z0-9+.-]*://", "", url.strip(), flags=re.I)
    url = re.sub(r"^[^@/]+@", "", url)
    if re.match(r"^[^/:]+:[^/\d]", url):  # scp style host:owner/repo (not host:port/...)
        url = url.replace(":", "/", 1)
    return re.sub(r"(\.git)?/*$", "", url).lower()


def head_of(folder):
    """(branch, commit) a folder has checked out, from one git call; ("", "") outside a repository."""
    out = git(folder, "rev-parse", "HEAD", "--abbrev-ref", "HEAD").splitlines()
    return (out[1], out[0]) if len(out) == 2 else ("", "")


def repo_of(folder):
    """(repository, branch, commit, top folder) of a folder. The repository is named by its origin remote (else
    by its top folder), so every checkout of one repository and branch on this machine shares one map."""
    folder = Path(folder).resolve()
    top = git(folder, "rev-parse", "--show-toplevel")
    if not top:
        return str(folder), "", "", folder
    remote = git(folder, "config", "--get", "remote.origin.url")
    branch, commit = head_of(folder)
    return (clean_remote(remote) if remote else str(Path(top).resolve())), branch, commit, Path(top).resolve()


def map_key(repo, branch):
    return hashlib.sha256(("%s\0%s" % (repo, branch)).encode("utf-8")).hexdigest()[:16]


def source_files(top):
    """The repository's source files, relative with forward slashes: git's list (tracked files, and new ones it
    does not ignore), else a walk; never inside SKIP_DIRS; at most MAX_FILES."""
    listed = git(top, "ls-files", "-co", "--exclude-standard", "-z")
    paths = [p for p in listed.split("\0") if p] if listed else []
    if not listed:
        for root, dirs, files in os.walk(top):
            dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and not d.startswith("."))
            paths += [os.path.relpath(os.path.join(root, f), top) for f in sorted(files)]
            if len(paths) > MAX_FILES * 10:  # a folder of everything (a home folder): enough
                break
    out = []
    for rel in paths:
        rel = rel.replace("\\", "/")
        if wanted(rel):
            out.append(rel)
            if len(out) >= MAX_FILES:
                break
    return out


def wanted(rel):
    """Is this path (relative, forward slashes) a source file the map reads?"""
    return os.path.splitext(rel)[1].lower() in LANGS and not SKIP_DIRS.intersection(rel.split("/")[:-1])


def line_of(text, index):
    return text.count("\n", 0, index) + 1


# --------------------------------------------------------------------------------------------
# Readers: each returns (definitions, references)
#   definitions  [(kind, name, line)]        an Angular component's name is "Name|selector"
#   references   [(kind, target, certain)]   target is a list for _resolve: ["py", module, name], ["jvm", ...]
# --------------------------------------------------------------------------------------------
def read_python(rel, text):
    """(definitions, references, signatures): signatures is None for a file that does not parse (someone is in
    the middle of an edit), so what it had stays."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return [], [], None
    defs, refs, sigs = [], [], {}
    if rel.endswith("/__init__.py") or rel == "__init__.py":
        package = rel[:-len("__init__.py")].rstrip("/").replace("/", ".")
    else:
        package = rel.rsplit("/", 1)[0].replace("/", ".") if "/" in rel else ""
    names = {}  # a name this file imported -> (module, name or "") it came from
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                refs.append(("imports", ["py", alias.name, ""], 1))
                names[alias.asname or alias.name.split(".")[0]] = (alias.name if alias.asname
                                                                   else alias.name.split(".")[0], "")
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parts = [p for p in package.split(".") if p]
                parts = parts[:max(0, len(parts) - node.level + 1)]
                base = ".".join(parts + ([node.module] if node.module else []))
            for alias in node.names:
                refs.append(("imports", ["py", base, "" if alias.name == "*" else alias.name], 1))
                if alias.name != "*":
                    names[alias.asname or alias.name] = (base, alias.name)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            defs.append(("function", node.name, node.lineno))
            sigs[node.name] = parameters(node)
        elif isinstance(node, ast.ClassDef):
            defs.append(("class", node.name, node.lineno))
            sigs[node.name] = "class"
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    defs.append(("method", "%s.%s" % (node.name, item.name), item.lineno))
                    sigs["%s.%s" % (node.name, item.name)] = parameters(item)
    # Calls that can be resolved: f() for an f imported by name, m.f() for a module m imported.
    called = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Name) and func.id in names and names[func.id][1]:
            called.add(names[func.id])
        elif isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name) and func.value.id in names:
            module, name = names[func.value.id]
            called.add(((module + "." + name) if name else module, func.attr))
    refs += [("calls", ["py", module, name], 1) for module, name in sorted(called)]
    return defs, refs, sigs


def parameters(fn):
    """A function's parameters as written, (a, b=1, *, c), and its return annotation."""
    returns = " -> %s" % ast.unparse(fn.returns) if fn.returns is not None else ""
    return "(%s)%s" % (ast.unparse(fn.args), returns)


def public(name):
    """A name other code may use: none of its parts starts with _, except dunders such as __init__."""
    return not any(part.startswith("_") and not (part.startswith("__") and part.endswith("__"))
                   for part in name.split("."))


def signature_changes(old, new, limit=5):
    """What changed in a file's interface, as short lines: parameters changed, public names renamed or removed.
    New names are not a change to anyone using the file; a body changed alone is none at all."""
    def shown(name, sig):
        return "class %s" % name if sig == "class" else name + sig
    out = []
    for name in sorted(old):
        if public(name) and name in new and old[name] != new[name]:
            out.append("%s is now %s" % (shown(name, old[name]), shown(name, new[name])))
    added = [n for n in new if n not in old and public(n)]
    for name in sorted(n for n in old if n not in new and public(n)):
        scope = name.rpartition(".")[0]
        twins = [a for a in added if new[a] == old[name] and a.rpartition(".")[0] == scope]
        if len(twins) == 1:  # the same parameters under a new name, in the same class: renamed
            out.append("%s was renamed %s" % (shown(name, old[name]), twins[0]))
            added.remove(twins[0])
        else:
            out.append("%s was removed" % shown(name, old[name]))
    if len(out) > limit:
        out = out[:limit] + ["and %d more" % (len(out) - limit)]
    return out


JVM_PACKAGE = re.compile(r"^\s*package\s+([\w.]+)", re.M)
JVM_IMPORT = re.compile(r"^\s*import\s+(static\s+)?([\w.]+?)(\.\*)?\s*(?:\bas\s+\w+)?\s*;?\s*$", re.M)
JAVA_TYPE = re.compile(r"^[ \t]*(?:@[\w.]+(?:\([^)]*\))?\s+)*(?:(?:public|protected|private|abstract|final|static|"
                       r"sealed|non-sealed|strictfp)\s+)*(class|interface|enum|record|@interface)\s+(\w+)", re.M)
JAVA_METHOD = re.compile(r"^[ \t]+(?:@[\w.]+(?:\([^)]*\))?\s+)*(?:(?:public|protected|private|static|final|abstract|"
                         r"synchronized|native|default)\s+)+(?:<[^>]+>\s+)?[\w<>\[\],.? ]+?\s+(\w+)\s*\(", re.M)
KOTLIN_TYPE = re.compile(r"^[ \t]*(?:@[\w.]+(?:\([^)]*\))?\s+)*(?:(?:public|internal|private|protected|open|abstract|"
                         r"sealed|data|enum|inner|annotation|value|inline|expect|actual|fun)\s+)*"
                         r"(class|interface|object)\s+(\w+)", re.M)
KOTLIN_FUN = re.compile(r"^[ \t]*(?:@[\w.]+(?:\([^)]*\))?\s+)*(?:(?:public|internal|private|protected|open|override|"
                        r"suspend|inline|operator|infix|tailrec|external|abstract|actual|expect)\s+)*"
                        r"fun\s+(?:<[^>]+>\s*)?(?:[\w.<>]+\.)?(\w+)\s*\(", re.M)
CAPITAL_NAME = re.compile(r"\b([A-Z]\w+)\b")
NOT_METHODS = {"if", "for", "while", "switch", "catch", "return", "new", "else", "throw", "synchronized"}


def read_jvm(rel, text, lang):
    defs, refs = [], []
    package = JVM_PACKAGE.search(text)
    defs.append(("package", package.group(1) if package else "", 0))
    for m in JVM_IMPORT.finditer(text):
        static, name, wildcard = m.group(1), m.group(2), m.group(3)
        if static and not wildcard:  # import static a.b.C.member: the class a.b.C
            name = name.rsplit(".", 1)[0]
        refs.append(("imports", ["jvm", name, bool(wildcard and not static)], 1))
    for m in (JAVA_TYPE if lang == "java" else KOTLIN_TYPE).finditer(text):
        kind = m.group(1) if m.group(1) in ("interface", "object") else "class"
        defs.append((kind, m.group(2), line_of(text, m.start())))
    for m in (JAVA_METHOD if lang == "java" else KOTLIN_FUN).finditer(text):
        if m.group(1) not in NOT_METHODS:
            defs.append(("function", m.group(1), line_of(text, m.start())))
    # Classes of the same package need no import: a capitalised name it mentions is a hint (certain = 0).
    body = JVM_IMPORT.sub("", text)
    refs.append(("uses", ["names", sorted(set(CAPITAL_NAME.findall(body)))], 0))
    return defs, refs


JS_IMPORT = re.compile(r"""(?:^|[;\s}])(?:import\s+(?:type\s+)?(?:[\w*{}\s,$]+?\s+from\s+)?|export\s+(?:type\s+)?"""
                       r"""(?:\*(?:\s+as\s+\w+)?|\{[^}]*\})\s+from\s+)['"]([^'"\n]+)['"]""", re.M)
JS_DYNAMIC = re.compile(r"""\b(?:require|import)\s*\(\s*['"]([^'"\n]+)['"]\s*\)""")
JS_EXPORT = re.compile(r"""^\s*export\s+(?:default\s+)?(?:declare\s+)?(?:abstract\s+)?(?:async\s+)?"""
                       r"""(function\*?|class|const|let|var|interface|type|enum)\s+(\w+)""", re.M)
ANGULAR = re.compile(r"""@(Component|NgModule|Injectable|Directive|Pipe)\s*\(\s*(\{.*?\})?\s*\)\s*"""
                     r"""(?:export\s+)?(?:default\s+)?(?:abstract\s+)?class\s+(\w+)""", re.S)
SELECTOR = re.compile(r"""selector\s*:\s*['"]([^'"]+)['"]""")
TEMPLATE_URL = re.compile(r"""templateUrl\s*:\s*['"]([^'"]+)['"]""")
TEMPLATE = re.compile(r"""\btemplate\s*:\s*`([^`]*)`""", re.S)
CONSTRUCTOR = re.compile(r"""\bconstructor\s*\(([^)]*)\)""", re.S)
INJECTED = re.compile(r"""(?:private|public|protected|readonly)\s+(?:readonly\s+)?\w+\s*:\s*(\w+)""")
INJECT_CALL = re.compile(r"""\binject\s*(?:<[^>]*>)?\(\s*(\w+)""")
TAG = re.compile(r"""<([a-z][a-z0-9]*-[a-z0-9-]+)""")
ANGULAR_KINDS = {"Component": "component", "NgModule": "module", "Injectable": "service", "Directive": "directive",
                 "Pipe": "pipe"}


def read_js(rel, text):
    defs, refs = [], []
    specs = [m.group(1) for m in JS_IMPORT.finditer(text)] + [m.group(1) for m in JS_DYNAMIC.finditer(text)]
    refs += [("imports", ["js", spec], 1) for spec in dict.fromkeys(specs)]
    for m in JS_EXPORT.finditer(text):
        word, name = m.group(1), m.group(2)
        kind = "component" if name[:1].isupper() and word in ("function", "function*", "class", "const") \
            else "class" if word in ("class", "interface", "type", "enum") else "function"
        defs.append((kind, name, line_of(text, m.start())))
    angular = False
    for m in ANGULAR.finditer(text):
        angular = True
        kind, body, name = ANGULAR_KINDS[m.group(1)], m.group(2) or "", m.group(3)
        selector = SELECTOR.search(body)
        defs = [d for d in defs if d[1] != name]  # the decorator says what it is better than its export does
        defs.append((kind, name + ("|" + selector.group(1) if selector and kind in ("component", "directive")
                                   else ""), line_of(text, m.start())))
        template = TEMPLATE.search(body)
        if template:
            refs += [("uses", ["tag", tag], 1) for tag in sorted(set(TAG.findall(template.group(1))))]
        url = TEMPLATE_URL.search(body)
        if url:
            refs.append(("uses", ["template", url.group(1)], 1))
        rest = text[m.end():m.end() + 20000]
        ctor = CONSTRUCTOR.search(rest)
        injected = INJECTED.findall(ctor.group(1)) if ctor else []
        for service in dict.fromkeys(injected + INJECT_CALL.findall(rest)):
            refs.append(("injects", ["name", service], 1))
    return defs, refs, angular


def read_html(rel, text):
    return [], [("uses", ["tag", tag], 1) for tag in sorted(set(TAG.findall(text)))]


def norm(path):
    """A relative path with forward slashes and no ./ or ..; a folder keeps its trailing /."""
    text = str(path).replace("\\", "/")
    out = os.path.normpath(text).replace("\\", "/") if text else ""
    out = "" if out == "." else out
    return out + "/" if text.endswith("/") and out and not out.endswith("/") else out


def like_prefix(folder):
    """A LIKE pattern (escape \\) for every path under folder."""
    folder = folder.rstrip("/")
    escaped = folder.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return escaped + "/%" if escaped else "%"


# --------------------------------------------------------------------------------------------
# The map of one repository and branch
# --------------------------------------------------------------------------------------------
class CodeMap:
    def __init__(self, path, top, repo="", branch=""):
        self.path, self.top, self.repo, self.branch = Path(path), Path(top), repo, branch
        self.lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(self.path), check_same_thread=False)
        # Derived data, built again when lost (docs/storage.md): no waiting on the disk at each commit, which
        # costs most of a one-file update on Windows.
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=OFF")
        self.db.executescript(SCHEMA)
        if self.meta("version") != MAP_VERSION:  # made by another version: build it again
            for table in ("nodes", "edges", "refs", "files", "signatures", "meta"):
                self.db.execute("DELETE FROM %s" % table)
            self._set_meta("version", MAP_VERSION)
            self.db.commit()
        self.base_url, self.aliases = self._ts_paths()
        self._linked = {}  # (a, b) -> linked(a, b), until the next update changes something
        self.changes = []  # [(path, signatures before, after)] since pop_changes, for C3's notices

    def close(self):
        with self.lock:
            self.db.close()

    def meta(self, key, default=""):
        row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default

    def _set_meta(self, key, value):
        self.db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, str(value)))

    def _ts_paths(self):
        """tsconfig's baseUrl and paths aliases, e.g. ("src", {"@app/*": ["src/app/*"]})."""
        for name in ("tsconfig.json", "tsconfig.base.json", "jsconfig.json"):
            try:
                text = (self.top / name).read_text(encoding="utf-8")
            except OSError:
                continue
            text = re.sub(r'("(?:\\.|[^"\\])*")|//[^\n]*|/\*.*?\*/', lambda m: m.group(1) or "", text, flags=re.S)
            text = re.sub(r",(\s*[}\]])", r"\1", text)
            try:
                options = json.loads(text).get("compilerOptions") or {}
            except (ValueError, AttributeError):
                continue
            base = norm(str(options.get("baseUrl") or "").rstrip("/"))
            paths = options.get("paths") if isinstance(options.get("paths"), dict) else {}
            return base, {alias: [norm(base + "/" + t if base else t) for t in targets if isinstance(t, str)]
                          for alias, targets in paths.items() if isinstance(targets, list)}
        return "", {}

    # Building ----------------------------------------------------------------------------
    def update(self, commit="", paths=None):
        """Read the files that are new or changed since the last update, forget the ones gone, and link again
        what that touches. With paths (files agents edited), only those are looked at: no git, so an edit is
        read at once. Returns the paths read, or None when a path is neither here nor known (it is another
        folder's): then the caller looks at everything."""
        started = time.time()
        with self.lock:
            if paths is None:
                files = source_files(self.top)
                known = {p: (m, s) for p, m, s in self.db.execute("SELECT path, mtime, size FROM files")}
            else:
                files = sorted({norm(p) for p in paths if wanted(norm(p))})
                known = {}
                for rel in files:
                    row = self.db.execute("SELECT mtime, size FROM files WHERE path = ?", (rel,)).fetchone()
                    if row:
                        known[rel] = tuple(row)
                    elif not (self.top / rel).is_file():
                        return None
            changed = []
            for rel in files:
                try:
                    st = (self.top / rel).stat()
                except OSError:
                    continue
                if st.st_size > MAX_FILE_BYTES:
                    continue
                if known.pop(rel, None) != (float(st.st_mtime), st.st_size):
                    changed.append((rel, st))
            gone = list(known)
            if changed or gone or not self.meta("built"):
                self._apply([rel for rel, _ in changed], gone, dict(changed))
                self._linked = {}
            self._set_meta("commit", commit)
            self._set_meta("repo", self.repo)
            self._set_meta("branch", self.branch)
            self._set_meta("built", time.time())
            self._set_meta("took", round(time.time() - started, 3))
            self.db.commit()
            return [rel for rel, _ in changed]

    def _apply(self, changed, gone, stats):
        before = {rel: self._definitions(rel) for rel in changed}
        # The files whose links point into what changes link again after it: their edges point at node ids
        # that are made anew.
        touched = sorted(set(changed) | set(gone))
        users = set()
        for start in range(0, len(touched), 500):
            chunk = touched[start:start + 500]
            users |= {p for (p,) in self.db.execute(
                "SELECT DISTINCT s.path FROM edges e JOIN nodes s ON s.id = e.src JOIN nodes d ON d.id = e.dst "
                "WHERE d.path IN (%s)" % ",".join("?" * len(chunk)), chunk)}
        for rel in gone:
            old = self.signatures(rel)
            if old:
                self.changes.append((rel, old, {}))
            self._forget(rel)
        new = [rel for rel in changed if not self._exists(rel)]
        for rel in changed:
            self._read(rel, stats[rel])
        # A file came or went, or what one defines changed: any reference anywhere may resolve differently now.
        everything = bool(new or gone) or any(self._definitions(rel) != before[rel] for rel in changed)
        self._link(None if everything else (set(changed) | users) - set(gone))

    def _exists(self, rel):
        return self.db.execute("SELECT 1 FROM files WHERE path = ?", (rel,)).fetchone() is not None

    def _definitions(self, rel):
        return sorted(self.db.execute("SELECT kind, name FROM nodes WHERE path = ?", (rel,)).fetchall())

    def _forget(self, rel):
        ids = [i for (i,) in self.db.execute("SELECT id FROM nodes WHERE path = ?", (rel,))]
        for start in range(0, len(ids), 400):
            chunk = ids[start:start + 400]
            marks = ",".join("?" * len(chunk))
            self.db.execute("DELETE FROM edges WHERE src IN (%s) OR dst IN (%s)" % (marks, marks), chunk + chunk)
        for table in ("nodes", "refs", "files", "signatures"):
            self.db.execute("DELETE FROM %s WHERE path = ?" % table, (rel,))

    def signatures(self, rel):
        """name -> parameters of each function, method and class of a Python file, as last read."""
        return dict(self.db.execute("SELECT name, signature FROM signatures WHERE path = ?", (rel,)).fetchall())

    def pop_changes(self):
        """The signature changes found since the last call: [(path, before, after)]."""
        with self.lock:
            out, self.changes = self.changes, []
        return out

    def _read(self, rel, st):
        known, old = self._exists(rel), self.signatures(rel)
        self._forget(rel)
        lang = LANGS[os.path.splitext(rel)[1].lower()]
        try:
            text = (self.top / rel).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return
        sigs = {}
        if lang == "python":
            defs, refs, sigs = read_python(rel, text)
            if sigs is None:  # does not parse now: keep what it had
                sigs = old
            elif known and sigs != old:
                self.changes.append((rel, old, sigs))
        elif lang in ("java", "kotlin"):
            defs, refs = read_jvm(rel, text, lang)
        elif lang == "html":
            defs, refs = read_html(rel, text)
        else:
            defs, refs, angular = read_js(rel, text)
            lang = "angular" if angular else lang
        self.db.execute("INSERT INTO nodes (kind, path, name, line, lang) VALUES ('file', ?, ?, 0, ?)", (rel, rel, lang))
        self.db.executemany("INSERT INTO nodes (kind, path, name, line, lang) VALUES (?, ?, ?, ?, ?)",
                            [(kind, rel, name, line, lang) for kind, name, line in defs])
        self.db.executemany("INSERT INTO refs (path, kind, target, certain) VALUES (?, ?, ?, ?)",
                            [(rel, kind, json.dumps(target), int(certain)) for kind, target, certain in refs])
        self.db.executemany("INSERT INTO signatures (path, name, signature) VALUES (?, ?, ?)",
                            [(rel, name, sig) for name, sig in sorted(sigs.items())])
        self.db.execute("INSERT OR REPLACE INTO files (path, mtime, size) VALUES (?, ?, ?)",
                        (rel, float(st.st_mtime), st.st_size))

    def _index(self):
        """What the map defines, in memory, for resolving references."""
        ix = {"files": {}, "defs": {}, "package_of": {}, "packages": {}, "types": {}, "names": {}, "selectors": {},
              "jvm_paths": {}}
        for i, kind, path, name, lang in self.db.execute("SELECT id, kind, path, name, lang FROM nodes ORDER BY id"):
            if kind == "file":
                ix["files"][path] = i
                continue
            bare, _, selector = name.partition("|")
            ix["defs"].setdefault(path, {}).setdefault(bare, i)
            if kind == "package":
                ix["package_of"][path] = bare
                ix["packages"].setdefault(bare, []).append(path)
            elif kind not in ("method", "function") or lang in ("react", "angular"):
                ix["names"].setdefault(bare, []).append(path)
            if selector:
                ix["selectors"].setdefault(selector, []).append(path)
        for path, package in ix["package_of"].items():
            stem = os.path.splitext(path.rsplit("/", 1)[-1])[0]
            for name in list(ix["defs"].get(path, {})) + [stem]:
                if name and name[:1].isupper():
                    ix["types"].setdefault((package + "." if package else "") + name, path)
            for root in JVM_ROOTS:
                at = path.find(root)
                if at == 0 or at > 0 and path[at - 1] == "/":
                    ix["jvm_paths"].setdefault(os.path.splitext(path[at + len(root):])[0].replace("/", "."), path)
                    break
        return ix

    def _link(self, only=None):
        """Turn references into edges: every file's (only=None), or only these files'."""
        ix = self._index()
        if only is None:
            self.db.execute("DELETE FROM edges")
            rows = self.db.execute("SELECT path, kind, target, certain FROM refs").fetchall()
        else:
            ids = [ix["files"][p] for p in only if p in ix["files"]]
            for start in range(0, len(ids), 500):
                chunk = ids[start:start + 500]
                self.db.execute("DELETE FROM edges WHERE src IN (%s)" % ",".join("?" * len(chunk)), chunk)
            rows = []
            for path in sorted(only):
                rows += self.db.execute("SELECT path, kind, target, certain FROM refs WHERE path = ?", (path,)).fetchall()
        best = {}  # (src, dst, kind) -> certain: one link per pair and kind, a certain one over a guess
        for path, kind, target, certain in rows:
            src = ix["files"].get(path)
            if src is None:
                continue
            target = json.loads(target)
            found = []
            if target[0] == "template":  # the component uses what its template uses
                html = norm(os.path.dirname(path) + "/" + target[1])
                if html in ix["files"]:
                    found.append((ix["files"][html], True, "template"))
                    for (tag,) in self.db.execute("SELECT target FROM refs WHERE path = ?", (html,)).fetchall():
                        found += [(dst, sure, kind) for dst, sure in self._resolve(path, json.loads(tag), ix)]
            else:
                found = [(dst, sure, kind) for dst, sure in self._resolve(path, target, ix)]
            for dst, sure, how in found:
                if dst != src:
                    key = (src, dst, how)
                    best[key] = max(best.get(key, 0), int(bool(certain) and sure))
        self.db.executemany("INSERT INTO edges (src, dst, kind, certain) VALUES (?, ?, ?, ?)",
                            [(s, d, k, c) for (s, d, k), c in best.items()])

    def _resolve(self, path, target, ix):
        """[(node id, certain)] that a reference of the file at path points at."""
        files, kind = ix["files"], target[0]
        if kind == "py":
            module, name = target[1], target[2]
            here = path.rsplit("/", 1)[0] + "/" if "/" in path else ""
            for root in dict.fromkeys(PY_ROOTS + (here,)):
                base = (root + module.replace(".", "/")) if module else root.rstrip("/")
                if not base:
                    continue
                if name and base + "/" + name + ".py" in files:  # from package import module
                    return [(files[base + "/" + name + ".py"], True)]
                for candidate in (base + ".py", base + "/__init__.py"):
                    if candidate in files:
                        defined = ix["defs"].get(candidate, {}).get(name) if name else None
                        return [(defined or files[candidate], True)]
            return []
        if kind == "jvm":
            name, wildcard = target[1], target[2]
            if wildcard:
                return [(files[p], True) for p in ix["packages"].get(name, []) if p != path and p in files]
            parts = name.split(".")
            for end in range(len(parts), 1, -1):  # a.b.C.Inner: the file that defines a.b.C
                prefix = ".".join(parts[:end])
                found = ix["types"].get(prefix) or ix["jvm_paths"].get(prefix)
                if found:
                    return [(files[found], True)]
            parent, _, last = name.rpartition(".")  # import a.b.topLevelFunction (Kotlin)
            hits = [p for p in ix["packages"].get(parent, []) if last in ix["defs"].get(p, {})]
            return [(ix["defs"][p][last], True) for p in hits[:MAX_GUESSES]]
        if kind == "names":  # capitalised names a JVM file mentions: classes of its own package
            package = ix["package_of"].get(path)
            if package is None:
                return []
            out = []
            for word in target[1]:
                found = ix["types"].get((package + "." if package else "") + word)
                if found and found != path:
                    out.append((files[found], False))
            return out
        if kind == "js":
            found = self._js_target(path, target[1], files)
            return [(files[found], True)] if found else []
        if kind == "tag":
            hits = ix["selectors"].get(target[1], [])
            return [(files[p], len(hits) == 1) for p in hits[:MAX_GUESSES]]
        if kind == "name":
            hits = [p for p in ix["names"].get(target[1], []) if p != path]
            return [(files[p], len(hits) == 1) for p in hits] if len(hits) <= MAX_GUESSES else []
        return []

    def _js_target(self, path, spec, files):
        bases = []
        if spec.startswith("."):
            bases.append(norm(os.path.dirname(path) + "/" + spec))
        else:
            for alias, targets in self.aliases.items():
                if alias.endswith("*") and spec.startswith(alias[:-1]):
                    rest = spec[len(alias) - 1:]
                    bases += [norm(t[:-1] + rest) if t.endswith("*") else t for t in targets]
                elif spec == alias:
                    bases += targets
            if self.base_url:
                bases.append(norm(self.base_url + "/" + spec))
        for base in bases:
            for candidate in [base] + [base + e for e in JS_EXTS] + [base + "/index" + e for e in JS_EXTS]:
                if candidate in files:
                    return candidate
        return ""

    # Questions -----------------------------------------------------------------------------
    def answer(self, value):
        """Every answer says which commit the map was built from."""
        return dict({"commit": self.meta("commit"), "repo": self.repo, "branch": self.branch}, **value)

    def _links(self, path, out, certain_only):
        here, there = ("src", "dst") if out else ("dst", "src")
        rows = self.db.execute(
            "SELECT o.path, e.kind, MAX(e.certain) FROM nodes f JOIN edges e ON e.%s = f.id JOIN nodes o ON o.id = e.%s "
            "WHERE f.path = ?%s GROUP BY o.path, e.kind ORDER BY o.path, e.kind"
            % (here, there, " AND e.certain = 1" if certain_only else ""), (path,)).fetchall()
        return [{"path": p, "kind": k, "certain": bool(c)} for p, k, c in rows if p != path]

    def imports(self, path, certain_only=False):
        """The files this one uses: imports, calls into, uses a component of, injects."""
        with self.lock:
            return self.answer({"path": norm(path), "files": self._links(norm(path), True, certain_only)})

    def importers(self, path, certain_only=False):
        """The files that use this one."""
        with self.lock:
            return self.answer({"path": norm(path), "files": self._links(norm(path), False, certain_only)})

    def linked(self, a, b, wait=True):
        """Is there a certain link, either way, between a and b? Each is an area as tasks give them: a path
        prefix, a file or every file under a folder. For C2's check of whether two tasks' areas overlap.
        wait=False never waits for an update in progress: it answers from what it knew."""
        return self.link(a, b, wait) == CERTAIN

    def link(self, a, b, wait=True):
        """How a and b are linked, either way: CERTAIN, GUESS (only links the readers guessed, C4's soft hint) or
        0. Cached until the next update changes something."""
        a, b = norm(a).rstrip("/"), norm(b).rstrip("/")
        if not wait and (a, b) in self._linked:
            return self._linked[(a, b)]
        if not self.lock.acquire(blocking=wait):  # being updated: what it said before, else no link
            return self._linked.get((a, b), 0)
        try:
            if (a, b) not in self._linked:
                under = "(%s = ? OR %s LIKE ? ESCAPE '\\')"
                sa, da, sb, db = under % ("s.path", "s.path"), under % ("d.path", "d.path"), \
                    under % ("s.path", "s.path"), under % ("d.path", "d.path")
                va, vb = [a, like_prefix(a)], [b, like_prefix(b)]
                row = self.db.execute(
                    "SELECT MAX(e.certain) FROM edges e JOIN nodes s ON s.id = e.src JOIN nodes d ON d.id = e.dst "
                    "WHERE (%s AND %s) OR (%s AND %s)" % (sa, db, sb, da), va + vb + vb + va).fetchone()
                how = 0 if row is None or row[0] is None else CERTAIN if row[0] else GUESS
                self._linked[(a, b)] = self._linked[(b, a)] = how
            return self._linked[(a, b)]
        finally:
            self.lock.release()

    def definitions(self, path):
        with self.lock:
            rows = self.db.execute("SELECT kind, name, line FROM nodes WHERE path = ? AND kind NOT IN ('file', "
                                   "'package') ORDER BY line, name", (norm(path),)).fetchall()
            return self.answer({"path": norm(path), "definitions": [{"kind": k, "name": n.split("|")[0], "line": l}
                                                                    for k, n, l in rows]})

    def status(self):
        with self.lock:
            langs = dict(self.db.execute("SELECT lang, COUNT(*) FROM nodes WHERE kind = 'file' GROUP BY lang"))
            edges = self.db.execute("SELECT COUNT(*), COALESCE(SUM(certain), 0) FROM edges").fetchone()
            return self.answer({"files": sum(langs.values()), "langs": langs, "links": edges[0], "certain": edges[1],
                                "built": float(self.meta("built") or 0), "took": float(self.meta("took") or 0),
                                "folder": str(self.top)})


# --------------------------------------------------------------------------------------------
# A project's maps
# --------------------------------------------------------------------------------------------
class Maps:
    """The maps of a project's connected folders on this machine: one per repository and branch, shared by every
    folder that is a checkout of it. check() brings them up to date; the server calls it when agents edit
    files and now and then to see whether a folder's HEAD moved."""

    def __init__(self, root):
        self.dir = Path(root) / "maps"
        self.maps = {}  # key -> CodeMap
        self.seen = {}  # folder -> (the (branch, commit) its map was last brought up to date at, the map, where
        # the folder is in its repository: "" or "app/")
        self.tops = {}  # folder -> (its map, where the folder is in its repository), for linked()
        self.lock = threading.Lock()

    def for_folder(self, folder):
        """(the folder's map, its commit)."""
        repo, branch, commit, top = repo_of(folder)
        key = map_key(repo, branch)
        with self.lock:
            cmap = self.maps.get(key)
            if cmap is not None and not cmap.path.exists():  # deleted: built again
                cmap.close()
                cmap = None
            if cmap is None:
                cmap = self.maps[key] = CodeMap(self.dir / ("%s.sqlite" % key), top, repo, branch)
        return cmap, commit

    def check(self, folders, edited=False, paths=()):
        """Bring up to date the map of each folder whose files were edited, whose HEAD moved (a commit, a pull,
        another branch), or that has none yet. paths: the files edited, relative to the folders, read alone
        when nothing else moved. Returns {folder: paths read, or why it could not}."""
        done = {}
        for folder in folders:
            folder = str(folder)
            head = head_of(folder)
            seen = self.seen.get(folder)
            try:
                if seen and seen[0] == head and seen[1].path.exists():
                    if not edited:
                        continue
                    if paths:
                        read = seen[1].update(head[1], [seen[2] + p for p in paths])
                        if read is not None:
                            done[folder] = read
                            continue
                cmap, commit = self.for_folder(folder)
                done[folder] = cmap.update(commit)
                inside = git(folder, "rev-parse", "--show-prefix")  # where the folder is in its repository
                self.seen[folder] = (head, cmap, inside)
                with self.lock:
                    self.tops[folder] = (cmap, inside)
            except (OSError, sqlite3.Error) as e:
                done[folder] = "could not map: %s" % e
        return done

    def find(self, folders, path=""):
        """The map a path belongs to, and the path relative to that map's repository: (CodeMap, rel), or
        (None, path) when no folder has one. An absolute path picks the folder it is in; a relative one is
        taken as relative to the first folder's repository."""
        for folder in folders:
            try:
                cmap, _ = self.for_folder(folder)
            except (OSError, sqlite3.Error):
                continue
            if not path:
                return cmap, ""
            p = Path(path)
            if not p.is_absolute():
                return cmap, norm(path)
            try:
                return cmap, p.resolve().relative_to(cmap.top).as_posix()
            except ValueError:
                continue
        return None, path

    def linked(self, a, b):
        """Is there a certain link between two areas (paths relative to a project folder) in any folder's map?
        Only maps already built are asked, and no git is run: cheap enough for every look at the task sheet."""
        return self.link(a, b) == CERTAIN

    def link(self, a, b):
        """The strongest link between two areas in any folder's map: CERTAIN, GUESS or 0 (as linked, no git)."""
        with self.lock:
            tops = list(self.tops.values())
        best = 0
        for cmap, inside in tops:
            try:
                best = max(best, cmap.link(inside + a, inside + b, wait=False))
            except sqlite3.Error:  # a map being built again (its file was deleted)
                continue
            if best == CERTAIN:
                break
        return best

    def insides(self, cmap):
        """Where the project's folders that use this map are in its repository: "" or "app/" each."""
        with self.lock:
            return sorted({inside for m, inside in self.tops.values() if m is cmap})

    def all(self):
        with self.lock:
            return list(self.maps.values())

    def close(self):
        with self.lock:
            for cmap in self.maps.values():
                cmap.close()
            self.maps = {}
