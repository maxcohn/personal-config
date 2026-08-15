"""Tier 1a: validate this repo's real packages.json and module manifests.

These catch a typo in the data before it reaches a machine, and guard invariants
sync.py assumes but doesn't itself enforce.
"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import sync  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parent.parent
KNOWN_METHODS = set(sync.SYSTEM_MANAGERS) | set(sync.LANG_INSTALLERS) | {"release", "build"}
KNOWN_KEYS = KNOWN_METHODS | sync.NON_METHOD_KEYS


MODULES_DIR = REPO_ROOT / "modules"


def modules():
    return sorted(p for p in MODULES_DIR.iterdir()
                  if p.is_dir() and (p / "manifest.json").is_file())


class TestModuleManifests(unittest.TestCase):
    def test_at_least_one_module_exists(self):
        self.assertTrue(modules(), "no modules found -- discovery is broken")

    def test_every_module_dir_has_a_manifest(self):
        """A directory under modules/ without a manifest is silently ignored."""
        for entry in MODULES_DIR.iterdir():
            if entry.is_dir():
                with self.subTest(directory=entry.name):
                    self.assertTrue((entry / "manifest.json").is_file(),
                                    "module directory has no manifest.json")

    def test_discovery_matches_directory_listing(self):
        discovered = {m.name for m in sync.load_modules()}
        on_disk = {p.name for p in modules()}
        self.assertEqual(discovered, on_disk)

    def test_manifests_are_valid(self):
        for mod in modules():
            with self.subTest(module=mod.name):
                data = json.loads((mod / "manifest.json").read_text())
                self.assertIn("destination", data)
                self.assertIsInstance(data["destination"], str)
                self.assertTrue(data["destination"].startswith("~"),
                                "destination should be home-relative")
                ignore = data.get("ignore", [])
                self.assertIsInstance(ignore, list)
                self.assertTrue(all(isinstance(p, str) for p in ignore))

    def test_modules_are_loadable_and_nonempty(self):
        for mod in sync.load_modules():
            with self.subTest(module=mod.name):
                self.assertTrue(mod.files(), "module has no managed files")

    def test_no_ignored_file_is_tracked(self):
        """An ignored file sitting in the repo would never sync -- a trap."""
        for mod in sync.load_modules():
            for rel in mod.path.rglob("*"):
                if rel.is_file():
                    relative = rel.relative_to(mod.path)
                    with self.subTest(module=mod.name, file=str(relative)):
                        if relative.as_posix() != "manifest.json":
                            self.assertFalse(mod.is_ignored(relative),
                                             "tracked file matches an ignore pattern")


class TestPackagesJson(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.packages = json.loads((REPO_ROOT / "packages.json").read_text())

    def entries(self):
        return self.packages.items()

    def test_entries_are_objects(self):
        for name, entry in self.entries():
            with self.subTest(package=name):
                self.assertIsInstance(entry, dict)

    def test_only_known_keys(self):
        for name, entry in self.entries():
            for key in entry:
                with self.subTest(package=name, key=key):
                    self.assertIn(key, KNOWN_KEYS, "unknown key -- typo?")

    def test_cask_prefix_only_under_brew(self):
        for name, entry in self.entries():
            for key, value in entry.items():
                if isinstance(value, str) and value.startswith("cask:"):
                    with self.subTest(package=name, key=key):
                        self.assertEqual(key, "brew",
                                         "cask: is meaningless outside brew")

    def test_aur_prefix_only_under_pacman(self):
        for name, entry in self.entries():
            for key, value in entry.items():
                if isinstance(value, str) and value.startswith("aur:"):
                    with self.subTest(package=name, key=key):
                        self.assertEqual(key, "pacman",
                                         "aur: is meaningless outside pacman")

    def test_system_manager_values_are_string_or_null(self):
        for name, entry in self.entries():
            for mgr in sync.SYSTEM_MANAGERS:
                if mgr in entry:
                    with self.subTest(package=name, manager=mgr):
                        self.assertIsInstance(entry[mgr], (str, type(None)))

    def test_release_entries_are_complete(self):
        for name, entry in self.entries():
            spec = entry.get("release")
            if spec is None:
                continue
            with self.subTest(package=name):
                for field in ("url", "version", "bin"):
                    self.assertIn(field, spec)
                self.assertIn("{version}", spec["url"],
                              "pinned version should appear in the URL")
                self.assertIsInstance(spec["version"], str)

    def test_release_urls_resolve_to_a_concrete_url(self):
        """No leftover unsubstituted placeholders after templating."""
        for name, entry in self.entries():
            if "release" not in entry:
                continue
            with self.subTest(package=name):
                url, binpath = sync.release_url_and_bin(name, entry["release"])
                self.assertNotIn("{", url)
                self.assertTrue(url.startswith("http"))
                self.assertNotIn("{", binpath)

    def test_build_entries_have_git_and_ref(self):
        for name, entry in self.entries():
            spec = entry.get("build")
            if spec is None:
                continue
            with self.subTest(package=name):
                self.assertIn("git", spec)
                self.assertIn("ref", spec, "builds must be pinned")

    def test_prefer_names_a_present_method(self):
        for name, entry in self.entries():
            prefer = entry.get("prefer")
            if prefer is None:
                continue
            with self.subTest(package=name):
                self.assertIn(prefer, KNOWN_METHODS)
                self.assertIn(prefer, entry, "prefer names a method not on this entry")

    def test_every_entry_installable_somewhere(self):
        """An entry with every method nulled out is dead weight."""
        for name, entry in self.entries():
            with self.subTest(package=name):
                usable = [k for k in KNOWN_METHODS
                          if k not in entry or entry.get(k) is not None]
                self.assertTrue(usable, "no method can ever install this")


if __name__ == "__main__":
    unittest.main()
