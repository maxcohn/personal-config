"""Tier 1a: validate this repo's real packages.json and module manifests.

These catch a typo in the data before it reaches a machine, and guard invariants
sync.py assumes but doesn't itself enforce.
"""

import json
import sys
import unittest
from pathlib import Path
from unittest import mock

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

    def test_language_installer_values_are_strings(self):
        for name, entry in self.entries():
            for lang in sync.LANG_INSTALLERS:
                if lang in entry:
                    with self.subTest(package=name, installer=lang):
                        self.assertIsInstance(entry[lang], (str, type(None)))

    def test_npm_entries_are_pinned(self):
        """sync.py doesn't re-check at runtime, so an unpinned npm spec -- which
        would install whatever "latest" is that day -- has to fail here."""
        for name, entry in self.entries():
            spec = entry.get("npm")
            if spec is None:
                continue
            with self.subTest(package=name):
                self.assertTrue(sync.npm_is_pinned(spec),
                                "npm value needs an explicit @version")

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
        """Methods are never inferred, so an entry naming none is dead weight."""
        for name, entry in self.entries():
            with self.subTest(package=name):
                usable = [k for k in KNOWN_METHODS if entry.get(k) is not None]
                self.assertTrue(usable, "no method can ever install this")


class TestRepoBlocks(unittest.TestCase):
    """Third-party repos install a root-level signing key, so the data is checked
    hard here rather than discovered on a machine."""

    @classmethod
    def setUpClass(cls):
        cls.packages = json.loads((REPO_ROOT / "packages.json").read_text())

    def apt_blocks(self):
        for name, entry in self.packages.items():
            spec = (entry.get("repo") or {}).get("apt")
            if spec is not None:
                yield name, entry, spec

    def test_repo_is_keyed_by_a_known_manager(self):
        for name, entry in self.packages.items():
            block = entry.get("repo")
            if block is None:
                continue
            with self.subTest(package=name):
                self.assertIsInstance(block, dict)
                for mgr in block:
                    self.assertIn(mgr, sync.SYSTEM_MANAGERS, "unknown manager -- typo?")

    def test_apt_blocks_are_complete(self):
        allowed = set(sync.APT_REQUIRED) | {"types", "architectures", "file"}
        for name, _entry, spec in self.apt_blocks():
            with self.subTest(package=name):
                for field in sync.APT_REQUIRED:
                    self.assertIn(field, spec)
                self.assertFalse(set(spec) - allowed, "unknown field(s) -- typo?")

    def test_urls_are_https(self):
        for name, _entry, spec in self.apt_blocks():
            with self.subTest(package=name):
                self.assertTrue(spec["uris"].startswith("https://"))
                self.assertTrue(spec["key_url"].startswith("https://"))

    def test_repo_implies_the_manager_names_a_package(self):
        """A repo with nothing to install from it is dead weight."""
        for name, entry, _spec in self.apt_blocks():
            with self.subTest(package=name):
                self.assertIsInstance(entry.get("apt"), str)

    def test_key_files_exist_and_are_armored(self):
        for name, _entry, spec in self.apt_blocks():
            with self.subTest(package=name):
                path = REPO_ROOT / spec["key"]
                self.assertTrue(path.is_file(), "run: ./sync.py repos fetch-key " + name)
                self.assertEqual(path.parent, REPO_ROOT / "keys")
                text = path.read_text()
                self.assertTrue(text.startswith("-----BEGIN PGP PUBLIC KEY BLOCK-----\n"))
                self.assertTrue(text.endswith("-----END PGP PUBLIC KEY BLOCK-----\n"))
                self.assertNotIn("\r", text, "CRLF would drift forever")

    def test_key_armor_is_intact(self):
        """Re-armoring the decoded body must reproduce the file, so a truncated
        paste or a bad CRC is caught here and not by apt on a fresh machine."""
        import base64
        for name, _entry, spec in self.apt_blocks():
            with self.subTest(package=name):
                text = (REPO_ROOT / spec["key"]).read_text()
                body = "".join(line for line in text.splitlines()[2:]
                               if line and not line.startswith(("=", "-")))
                self.assertEqual(sync.enarmor(base64.b64decode(body)), text)

    def test_no_orphan_keys(self):
        keys_dir = REPO_ROOT / "keys"
        if not keys_dir.is_dir():
            return
        referenced = {(REPO_ROOT / spec["key"]).resolve()
                      for _n, _e, spec in self.apt_blocks()}
        for path in keys_dir.iterdir():
            if path.is_file():
                with self.subTest(key=path.name):
                    self.assertIn(path.resolve(), referenced, "key is unreferenced")

    def test_specs_render_without_error(self):
        """"auto" is resolved with stubs: this asserts the data is renderable, not
        that whoever runs the suite is sitting on Ubuntu with dpkg installed."""
        with mock.patch.object(sync, "os_release_codename", lambda: "noble"), \
             mock.patch.object(sync, "dpkg_architecture", lambda: "amd64"):
            for name, _entry, spec in self.apt_blocks():
                with self.subTest(package=name):
                    rendered = sync.render_apt_source(sync.apt_repo_name(name, spec), spec)
                    self.assertIn("Signed-By: /etc/apt/keyrings/", rendered)
                    self.assertTrue(rendered.endswith("\n"))

    def test_no_conflicting_repo_definitions(self):
        """Two entries may share a repo name only if they render identically."""
        with mock.patch.object(sync, "os_release_codename", lambda: "noble"), \
             mock.patch.object(sync, "dpkg_architecture", lambda: "amd64"):
            sync.repo_specs(self.packages, "apt")


if __name__ == "__main__":
    unittest.main()
