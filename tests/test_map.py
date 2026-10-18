"""Tests for the code map (crewchat_map.py, roadmap C1): small sample repositories in each language it reads,
checked against hand-made lists of what links to what."""
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import crewchat_map  # noqa: E402

HAS_GIT = shutil.which("git") is not None


def write(top, files):
    for rel, text in files.items():
        path = Path(top) / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


class Sample(unittest.TestCase):
    """A sample repository in a temporary folder, mapped."""

    FILES = {}

    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="crewchat-map-"))
        self.top = self.dir / "repo"
        write(self.top, self.FILES)
        self.maps = crewchat_map.Maps(self.dir / "project")
        self.maps.check([self.top])
        self.map, _ = self.maps.for_folder(self.top)

    def tearDown(self):
        self.maps.close()
        shutil.rmtree(self.dir, ignore_errors=True)

    def links(self, certain_only=True):
        """{file: {file it links to}} for every file of the sample: the map's answer, to check against a list."""
        out = {}
        for rel in self.FILES:
            found = {f["path"] for f in self.map.imports(rel, certain_only)["files"]}
            if found:
                out[rel] = found
        return out


class PythonRepo(Sample):
    FILES = {
        "app/__init__.py": "",
        "app/models.py": "class User:\n    def name(self, full=True):\n        return ''\n\n\ndef make(id, *, admin=False):\n"
                         "    return User()\n",
        "app/views.py": "from .models import make, User\nfrom . import util\n\n\ndef show():\n    return make(1)\n",
        "app/util.py": "import json\nimport os.path\n",
        "app/api/__init__.py": "from ..views import show\n",
        "app/api/routes.py": "from app.api import show\nimport app.util as u\n\n\ndef route():\n    u.helper()\n",
        "main.py": "import app.views\nfrom app import models\nmodels.make(2)\n",
        "scripts/run.py": "import helper\n",
        "scripts/helper.py": "",
        "broken.py": "def (:\n",
    }

    def test_imports_resolve_to_the_right_files(self):
        self.assertEqual(self.links(), {
            "app/views.py": {"app/models.py", "app/util.py"},
            "app/api/__init__.py": {"app/views.py"},
            "app/api/routes.py": {"app/api/__init__.py", "app/util.py"},
            "main.py": {"app/views.py", "app/models.py"},
            "scripts/run.py": {"scripts/helper.py"},  # a script's own folder is where its imports start
        })

    def test_definitions_and_calls(self):
        names = [(d["kind"], d["name"]) for d in self.map.definitions("app/models.py")["definitions"]]
        self.assertEqual(names, [("class", "User"), ("method", "User.name"), ("function", "make")])
        kinds = {(f["path"], f["kind"]) for f in self.map.imports("main.py")["files"]}
        self.assertIn(("app/models.py", "calls"), kinds)  # models.make(2): a call into make
        self.assertIn(("app/views.py", "calls"), {(f["path"], f["kind"]) for f in self.map.importers("app/models.py")["files"]})
        self.assertEqual(self.map.definitions("broken.py")["definitions"], [])  # a file it cannot parse: no harm

    def test_folders_are_linked_through_their_files(self):
        self.assertTrue(self.map.linked("app/api/", "app/views.py"))
        self.assertTrue(self.map.linked("app/models.py", "main.py"))  # either way round
        self.assertFalse(self.map.linked("scripts/", "app/"))
        self.assertFalse(self.map.linked("scripts/", "app_other/"))


class JavaRepo(Sample):
    FILES = {
        "app/src/main/java/com/shop/cart/Cart.java":
            "package com.shop.cart;\n\nimport com.shop.model.Item;\nimport com.shop.util.*;\n"
            "import static com.shop.util.Money.format;\n\n@Entity\npublic final class Cart {\n"
            "    public static <T> List<T> items(Item item) { return null; }\n    private Pricing pricing;\n}\n",
        "app/src/main/java/com/shop/cart/Pricing.java": "package com.shop.cart;\n\nclass Pricing {}\n",
        "app/src/main/java/com/shop/model/Item.java":
            "package com.shop.model;\n\npublic record Item(String name) {\n    public static class Builder {}\n}\n",
        "app/src/main/java/com/shop/util/Money.java": "package com.shop.util;\n\npublic class Money {}\n",
        "app/src/main/java/com/shop/util/Tax.java": "package com.shop.util;\n\npublic interface Tax {}\n",
        "app/src/main/java/com/shop/Main.java":
            "package com.shop;\n\nimport com.shop.model.Item.Builder;\nimport java.util.List;\n\n"
            "public class Main {}\n",
    }

    def test_imports_resolve_by_package(self):
        self.assertEqual(self.links(), {
            "app/src/main/java/com/shop/cart/Cart.java": {
                "app/src/main/java/com/shop/model/Item.java",  # a class
                "app/src/main/java/com/shop/util/Money.java",  # a wildcard: the package's files, and a static import
                "app/src/main/java/com/shop/util/Tax.java"},
            "app/src/main/java/com/shop/Main.java": {"app/src/main/java/com/shop/model/Item.java"},  # a nested class
        })

    def test_the_same_package_is_only_a_hint(self):
        links = self.map.imports("app/src/main/java/com/shop/cart/Cart.java")["files"]
        pricing = [f for f in links if f["path"].endswith("Pricing.java")]
        self.assertEqual([(f["kind"], f["certain"]) for f in pricing], [("uses", False)])
        self.assertFalse(self.map.linked("app/src/main/java/com/shop/cart/Cart.java",
                                         "app/src/main/java/com/shop/cart/Pricing.java"))

    def test_how_two_areas_are_linked(self):
        # C4: certain links (an import), guesses (a class of the same package, by name), or none.
        base = "app/src/main/java/com/shop/"
        self.assertEqual(self.map.link(base + "cart/Cart.java", base + "model/"), crewchat_map.CERTAIN)
        self.assertEqual(self.map.link(base + "cart/Pricing.java", base + "cart/Cart.java"), crewchat_map.GUESS)
        self.assertEqual(self.map.link(base + "model/", base + "util/Money.java"), 0)

    def test_definitions(self):
        names = {(d["kind"], d["name"]) for d in self.map.definitions("app/src/main/java/com/shop/cart/Cart.java")["definitions"]}
        self.assertEqual(names, {("class", "Cart"), ("function", "items")})
        names = {d["name"] for d in self.map.definitions("app/src/main/java/com/shop/model/Item.java")["definitions"]}
        self.assertEqual(names, {"Item", "Builder"})


class KotlinRepo(Sample):
    FILES = {
        "core/src/main/kotlin/io/app/data/Repo.kt":
            "package io.app.data\n\nimport io.app.net.Api\nimport io.app.net.fetchAll as fetch\n\n"
            "data class Repo(val api: Api) {\n    suspend fun load() = fetch()\n}\n\nobject Cache\n",
        "core/src/main/kotlin/io/app/net/Api.kt": "package io.app.net\n\ninterface Api\n",
        "core/src/main/kotlin/io/app/net/Calls.kt":
            "package io.app.net\n\nfun fetchAll(): List<String> = emptyList()\ninternal fun <T> List<T>.second() = this[1]\n",
        "core/src/main/kotlin/io/app/ui/Screen.kt":
            "package io.app.ui\n\nimport io.app.data.*\nimport io.app.data.Repo\n\nclass Screen(private val repo: Repo)\n",
        "build.gradle.kts": "plugins { kotlin(\"jvm\") }\n",
    }

    def test_imports_resolve_by_package(self):
        self.assertEqual(self.links(), {
            "core/src/main/kotlin/io/app/data/Repo.kt": {"core/src/main/kotlin/io/app/net/Api.kt",
                                                         "core/src/main/kotlin/io/app/net/Calls.kt"},  # a top level fun
            "core/src/main/kotlin/io/app/ui/Screen.kt": {"core/src/main/kotlin/io/app/data/Repo.kt"},
        })

    def test_definitions(self):
        names = {(d["kind"], d["name"]) for d in self.map.definitions("core/src/main/kotlin/io/app/data/Repo.kt")["definitions"]}
        self.assertEqual(names, {("class", "Repo"), ("function", "load"), ("object", "Cache")})
        names = {d["name"] for d in self.map.definitions("core/src/main/kotlin/io/app/net/Calls.kt")["definitions"]}
        self.assertEqual(names, {"fetchAll", "second"})


class ReactRepo(Sample):
    FILES = {
        "tsconfig.json": '{\n  // comments are allowed\n  "compilerOptions": {"baseUrl": ".", "paths": {"@ui/*": ["src/ui/*"],'
                         ' "@lib": ["src/lib/index.ts"]},},\n}\n',
        "src/App.tsx": "import React, { useState } from 'react';\nimport { Button } from './ui/Button';\n"
                       "import Header from \"@ui/Header\";\nimport type { Props } from '@lib';\nimport './App.css';\n"
                       "const Lazy = React.lazy(() => import('./pages/Settings'));\n"
                       "export default function App() { return null }\n",
        "src/ui/Button.jsx": "export const Button = () => null;\nexport function useClick() {}\n",
        "src/ui/Header.tsx": "import {\n  Button,\n} from './Button'\nexport class Header extends React.Component {}\n",
        "src/ui/index.ts": "export * from './Button';\nexport { Header } from './Header';\n",
        "src/lib/index.ts": "export interface Props { id: string }\n",
        "src/pages/Settings/index.js": "const util = require('../../lib');\nmodule.exports = {};\n",
        "src/node_modules/left-pad/index.js": "module.exports = 1;\n",
    }

    def test_imports_resolve_with_extensions_index_files_and_aliases(self):
        self.assertEqual(self.links(), {
            "src/App.tsx": {"src/ui/Button.jsx", "src/ui/Header.tsx", "src/lib/index.ts", "src/pages/Settings/index.js"},
            "src/ui/Header.tsx": {"src/ui/Button.jsx"},
            "src/ui/index.ts": {"src/ui/Button.jsx", "src/ui/Header.tsx"},  # a barrel's re-exports
            "src/pages/Settings/index.js": {"src/lib/index.ts"},
        })
        self.assertEqual(self.map.status()["files"], 6)  # not what is in node_modules/

    def test_components_start_with_a_capital(self):
        kinds = {d["name"]: d["kind"] for d in self.map.definitions("src/ui/Button.jsx")["definitions"]}
        self.assertEqual(kinds, {"Button": "component", "useClick": "function"})
        self.assertEqual(self.map.definitions("src/App.tsx")["definitions"][0]["kind"], "component")


class AngularRepo(Sample):
    FILES = {
        "src/app/cart/cart.component.ts":
            "import { Component } from '@angular/core';\nimport { CartService } from '../services/cart.service';\n\n"
            "@Component({\n  selector: 'app-cart',\n  templateUrl: './cart.component.html',\n  styles: [`:host { x: 1 }`],\n})\n"
            "export class CartComponent {\n  constructor(private readonly cart: CartService, public log: Logger) {}\n}\n",
        "src/app/cart/cart.component.html": "<div><app-price-tag [value]=\"total\"></app-price-tag></div>\n",
        "src/app/price/price-tag.component.ts":
            "@Component({ selector: 'app-price-tag', template: `<span>{{ value }}</span>` })\nexport class PriceTagComponent {}\n",
        "src/app/services/cart.service.ts": "@Injectable({ providedIn: 'root' })\nexport class CartService {}\n",
        "src/app/services/logger.ts": "@Injectable()\nexport class Logger {}\n",
        "src/app/shell.component.ts":
            "@Component({\n  selector: 'app-shell',\n  template: `<app-cart></app-cart><router-outlet></router-outlet>`\n})\n"
            "export class ShellComponent { private api = inject(CartService); }\n",
        "src/app/app.module.ts":
            "import { NgModule } from '@angular/core';\nimport { CartComponent } from './cart/cart.component';\n\n"
            "@NgModule({ declarations: [CartComponent] })\nexport class AppModule {}\n",
    }

    def test_templates_selectors_and_injection_link_files(self):
        self.assertEqual(self.links(), {
            "src/app/cart/cart.component.ts": {
                "src/app/services/cart.service.ts",  # imported, and injected
                "src/app/cart/cart.component.html",  # its template
                "src/app/price/price-tag.component.ts",  # <app-price-tag> in its template
                "src/app/services/logger.ts"},  # injected without an import (a name defined once)
            "src/app/cart/cart.component.html": {"src/app/price/price-tag.component.ts"},
            "src/app/shell.component.ts": {"src/app/cart/cart.component.ts", "src/app/services/cart.service.ts"},
            "src/app/app.module.ts": {"src/app/cart/cart.component.ts"},
        })

    def test_what_angular_declares(self):
        kinds = {d["name"]: d["kind"] for f in self.FILES if f.endswith(".ts")
                 for d in self.map.definitions(f)["definitions"]}
        self.assertEqual(kinds, {"CartComponent": "component", "PriceTagComponent": "component",
                                 "CartService": "service", "Logger": "service", "ShellComponent": "component",
                                 "AppModule": "module"})
        self.assertEqual(self.map.status()["langs"].get("angular"), 6)


class Updates(Sample):
    FILES = {"a.py": "import b\n", "b.py": "def f():\n    pass\n", "c.py": "", "pkg/__init__.py": ""}

    def test_an_edit_is_read_again_and_others_are_not(self):
        os.utime(self.top / "a.py", (time.time() + 5, time.time() + 5))
        self.assertEqual(self.map.update(), ["a.py"])
        self.assertEqual(self.map.update(), [])  # nothing changed since
        write(self.top, {"a.py": "import c\n"})
        os.utime(self.top / "a.py", (time.time() + 10, time.time() + 10))
        self.map.update()
        self.assertEqual({f["path"] for f in self.map.imports("a.py")["files"]}, {"c.py"})

    def test_links_into_a_changed_file_survive_it(self):
        write(self.top, {"b.py": "def f():\n    return 1\n"})  # same definitions: only a.py and b.py link again
        os.utime(self.top / "b.py", (time.time() + 5, time.time() + 5))
        self.map.update()
        self.assertTrue(self.map.linked("a.py", "b.py"))

    def test_new_and_gone_files(self):
        write(self.top, {"d.py": "from pkg import e\n"})
        self.map.update()
        self.assertEqual([f["path"] for f in self.map.imports("d.py")["files"]], ["pkg/__init__.py"])  # no pkg/e.py yet
        write(self.top, {"pkg/e.py": ""})
        self.map.update()
        self.assertTrue(self.map.linked("d.py", "pkg/e.py"))  # it resolves once the file appears
        (self.top / "pkg" / "e.py").unlink()
        self.map.update()
        self.assertFalse(self.map.linked("d.py", "pkg/e.py"))
        self.assertEqual(self.map.definitions("pkg/e.py")["definitions"], [])

    def test_a_map_of_another_version_is_built_again(self):
        self.map.db.execute("UPDATE meta SET value = '1' WHERE key = 'version'")
        self.map.db.commit()
        path = self.map.path
        self.maps.close()
        cmap = crewchat_map.CodeMap(path, self.top)
        try:
            self.assertEqual(cmap.status()["files"], 0)  # emptied, to be read again
            cmap.update()
            self.assertTrue(cmap.linked("a.py", "b.py"))
        finally:
            cmap.close()

    def test_deleting_the_map_builds_it_again(self):
        path = self.map.path
        self.maps.close()
        path.unlink()
        maps = crewchat_map.Maps(self.dir / "project")  # as after a restart
        try:
            maps.check([self.top])
            cmap, _ = maps.for_folder(self.top)
            self.assertTrue(path.exists())
            self.assertTrue(cmap.linked("a.py", "b.py"))
            cmap.close()
            path.unlink()  # deleted while the server runs: the next check builds it again
            self.assertIn(str(self.top), maps.check([self.top]))
            self.assertTrue(maps.for_folder(self.top)[0].linked("a.py", "b.py"))
        finally:
            maps.close()

    def test_an_edit_alone_is_read_without_looking_at_the_rest(self):
        write(self.top, {"c.py": "import b\n", "new.py": "import c\n"})  # c.py edited; new.py and d.md as well
        later = time.time() + 20
        for rel in ("c.py", "new.py"):
            os.utime(self.top / rel, (later, later))
        os.utime(self.top / "a.py", (later, later))  # changed too, but not said: left for the next full look
        self.assertEqual(self.map.update(paths=["c.py", "./new.py", "notes.md"]), ["c.py", "new.py"])
        self.assertTrue(self.map.linked("new.py", "c.py"))
        self.assertIsNone(self.map.update(paths=["elsewhere/x.py"]))  # not this folder's file: look at all
        self.assertEqual(self.maps.check([self.top], edited=True, paths=["elsewhere/x.py"]), {str(self.top): ["a.py"]})
        (self.top / "c.py").unlink()
        self.assertEqual(self.maps.check([self.top], edited=True, paths=["c.py"]), {str(self.top): []})
        self.assertFalse(self.map.linked("new.py", "c.py"))  # gone

    def test_a_check_reads_nothing_while_nothing_moved(self):
        self.assertEqual(self.maps.check([self.top]), {})
        self.assertEqual(list(self.maps.check([self.top], edited=True)), [str(self.top)])


class Signatures(Sample):
    """C3: a Python file's interface (functions, methods, classes and their parameters), and what changed in it."""

    FILES = {"api.py": "class Store:\n    def get(self, key, default=None):\n        pass\n\n    def _cache(self):\n"
                       "        pass\n\n\ndef read(path, *, strict=False) -> str:\n    return ''\n\n\ndef close():\n"
                       "    pass\n", "other.py": ""}

    def rewrite(self, text, rel="api.py"):
        write(self.top, {rel: text})
        os.utime(self.top / rel, (time.time() + 10, time.time() + 10))
        self.map.update()
        return self.map.pop_changes()

    def test_what_is_kept(self):
        self.assertEqual(self.map.signatures("api.py"), {
            "Store": "class", "Store.get": "(self, key, default=None)", "Store._cache": "(self)",
            "read": "(path, *, strict=False) -> str", "close": "()"})
        self.assertEqual(self.map.pop_changes(), [])  # a first read is no change

    def test_a_parameter_renamed_and_a_function_removed(self):
        text = (self.top / "api.py").read_text().replace("read(path,", "read(source,").replace("def close():", "def shut():")
        [(rel, before, after)] = self.rewrite(text)
        self.assertEqual(crewchat_map.signature_changes(before, after), [
            "read(path, *, strict=False) -> str is now read(source, *, strict=False) -> str",
            "close() was renamed shut"])

    def test_a_body_or_a_private_name_is_no_change(self):
        text = (self.top / "api.py").read_text().replace("return ''", "return 'x'").replace("_cache(self)", "_cache(self, n)")
        changes = self.rewrite(text)
        self.assertEqual([crewchat_map.signature_changes(b, a) for _, b, a in changes], [[]])

    def test_a_file_that_does_not_parse_keeps_what_it_had(self):
        before = self.map.signatures("api.py")
        self.assertEqual(self.rewrite("def read(:\n"), [])
        self.assertEqual(self.map.signatures("api.py"), before)

    def test_a_file_deleted_takes_its_names_with_it(self):
        (self.top / "api.py").unlink()
        self.map.update()
        [(rel, before, after)] = self.map.pop_changes()
        self.assertEqual((rel, after), ("api.py", {}))
        self.assertIn("class Store was removed", crewchat_map.signature_changes(before, after))

    def test_many_changes_are_cut_short(self):
        before = {"f%d" % i: "()" for i in range(8)}
        lines = crewchat_map.signature_changes(before, {})
        self.assertEqual((len(lines), lines[-1]), (6, "and 3 more"))
        self.assertEqual(crewchat_map.signature_changes({"Store.__init__": "(self)"}, {"Store.__init__": "(self, x)"}),
                         ["Store.__init__(self) is now Store.__init__(self, x)"])  # a constructor is public


@unittest.skipUnless(HAS_GIT, "needs git")
class Repositories(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="crewchat-map-git-"))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def git(self, folder, *args):
        subprocess.run(["git", "-C", str(folder), "-c", "user.email=t@example.com", "-c", "user.name=t",
                        "-c", "commit.gpgsign=false"] + list(args), check=True, capture_output=True)

    def test_one_map_per_repository_and_branch(self):
        top = self.dir / "one"
        write(top, {"a.py": "import b\n", "b.py": "", "build/gen.py": "", "ignored.py": "", ".gitignore": "ignored.py\n"})
        self.git(top, "init", "-q", "-b", "main")
        self.git(top, "remote", "add", "origin", "git@github.com:Owner/Shop.git")
        self.git(top, "add", "-A")
        self.git(top, "commit", "-qm", "one")
        other = self.dir / "two"  # a second checkout of the same repository and branch shares the map
        self.git(self.dir, "clone", "-q", str(top), str(other))
        self.git(other, "remote", "set-url", "origin", "https://github.com/owner/shop")
        maps = crewchat_map.Maps(self.dir / "project")
        try:
            maps.check([top, other])
            first, commit = maps.for_folder(top)
            self.assertIs(maps.for_folder(other)[0], first)
            self.assertEqual(first.repo, "github.com/owner/shop")
            self.assertEqual(first.imports("a.py")["commit"], commit)  # every answer says what it was built from
            self.assertEqual(len(commit), 40)
            self.assertEqual(first.status()["files"], 2)  # not build/, nor what git ignores
            self.git(top, "checkout", "-qb", "feature")
            self.assertEqual(list(maps.check([top])), [str(top)])  # HEAD moved: looked at again
            self.assertIsNot(maps.for_folder(top)[0], first)  # another branch, another map
            self.assertEqual(len(list((self.dir / "project" / "maps").glob("*.sqlite"))), 2)
            self.git(top, "commit", "-q", "--allow-empty", "-m", "two")
            maps.check([top])
            self.assertEqual(maps.for_folder(top)[0].status()["commit"], crewchat_map.head_of(top)[1])
        finally:
            maps.close()

    def test_remote_addresses_name_one_repository(self):
        for url in ("git@github.com:Owner/Shop.git", "https://github.com/owner/shop", "ssh://git@github.com/owner/shop.git/",
                    "https://user@github.com/owner/shop.git"):
            self.assertEqual(crewchat_map.clean_remote(url), "github.com/owner/shop", url)
        self.assertEqual(crewchat_map.clean_remote("ssh://git@host:2222/a/b.git"), "host:2222/a/b")


class Speed(unittest.TestCase):
    def test_the_crewchat_repository_maps_in_under_two_seconds(self):
        out = Path(tempfile.mkdtemp(prefix="crewchat-map-speed-"))
        try:
            maps = crewchat_map.Maps(out)
            started = time.time()
            maps.check([ROOT])
            took = time.time() - started
            cmap, _ = maps.for_folder(ROOT)
            self.assertLess(took, 2.0)
            self.assertGreater(cmap.status()["files"], 10)
            self.assertIn("crewchat_peers.py", {f["path"] for f in cmap.imports("tests/test_peers.py")["files"]})
            # One file edited: read again in under 100 ms.
            rel = "crewchat_peers.py"
            st = (ROOT / rel).stat()
            cmap.db.execute("UPDATE files SET mtime = ? WHERE path = ?", (st.st_mtime - 1, rel))  # as if it changed
            started = time.time()
            self.assertEqual(cmap.update(paths=[rel]), [rel])  # the file an agent edited, as the server passes it
            self.assertLess(time.time() - started, 0.1)
            maps.close()
        finally:
            shutil.rmtree(out, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
