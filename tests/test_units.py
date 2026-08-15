"""Tier 1a: in-process tests of sync.py helpers.

Covers logic the CLI can't reach on a given machine -- you can't run pacman or
brew on a Debian box, but their command construction still has to be right.
"""

import io
import json
import shutil
import sys
import tarfile
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import sync  # noqa: E402

from helpers import (make_malicious_tar, make_malicious_zip)  # noqa: E402


class TestResolve(unittest.TestCase):
    def resolve(self, entry, sysmgr="apt", have=()):
        """Resolve with shutil.which faked to only find the named tools."""
        with mock.patch.object(sync.shutil, "which", lambda t: t if t in have else None):
            return sync.resolve("thing", entry, sysmgr)

    def test_omitted_manager_defaults_to_entry_name(self):
        self.assertEqual(self.resolve({}), [("apt", "thing")])

    def test_rename(self):
        self.assertEqual(self.resolve({"apt": "thing-bin"}), [("apt", "thing-bin")])

    def test_null_skips_manager(self):
        self.assertEqual(self.resolve({"apt": None}), [])

    def test_no_system_manager_detected(self):
        self.assertEqual(self.resolve({}, sysmgr=None), [])

    def test_language_installer_requires_toolchain(self):
        entry = {"apt": None, "cargo": "thing@1.0"}
        self.assertEqual(self.resolve(entry), [], "cargo absent: must not be offered")
        self.assertEqual(self.resolve(entry, have=("cargo",)), [("cargo", "thing@1.0")])

    def test_method_ordering(self):
        entry = {"cargo": "thing", "release": {"version": "1"}, "build": {"git": "u"}}
        methods = [m for m, _ in self.resolve(entry, have=("cargo",))]
        self.assertEqual(methods, ["apt", "cargo", "release", "build"])

    def test_prefer_overrides_order(self):
        entry = {"cargo": "thing", "release": {"version": "1"}, "prefer": "release"}
        methods = [m for m, _ in self.resolve(entry, have=("cargo",))]
        self.assertEqual(methods[0], "release")

    def test_prefer_keeps_others_as_fallback(self):
        entry = {"release": {"version": "1"}, "prefer": "release"}
        methods = [m for m, _ in self.resolve(entry)]
        self.assertEqual(methods, ["release", "apt"])


class TestSystemInstallCommands(unittest.TestCase):
    """install_via_system builds shell commands we can't safely run here."""

    def run_install(self, mgr, specs, root=False, helpers=()):
        buf = io.StringIO()
        with mock.patch.object(sync.os, "geteuid", lambda: 0 if root else 1000), \
             mock.patch.object(sync.shutil, "which",
                               lambda t: t if t in helpers else None), \
             redirect_stdout(buf):
            sync.install_via_system(mgr, specs, dry_run=True)
        return buf.getvalue()

    def test_apt(self):
        out = self.run_install("apt", ["git", "vim"])
        self.assertIn("sudo apt install -y git vim", out)

    def test_pacman(self):
        out = self.run_install("pacman", ["git"])
        self.assertIn("sudo pacman -S --needed --noconfirm git", out)

    def test_dnf(self):
        out = self.run_install("dnf", ["git"])
        self.assertIn("sudo dnf install -y git", out)

    def test_brew_needs_no_sudo(self):
        out = self.run_install("brew", ["git"])
        self.assertIn("brew install git", out)
        self.assertNotIn("sudo", out)

    def test_root_drops_sudo(self):
        out = self.run_install("apt", ["git"], root=True)
        self.assertIn("apt install -y git", out)
        self.assertNotIn("sudo", out)

    def test_cask_split_out(self):
        out = self.run_install("brew", ["git", "cask:firefox"])
        self.assertIn("brew install git", out)
        self.assertIn("brew install --cask firefox", out)

    def test_aur_uses_helper_when_present(self):
        out = self.run_install("pacman", ["aur:thing-git"], helpers=("paru",))
        self.assertIn("paru -S --needed --noconfirm thing-git", out)

    def test_aur_without_helper_reports_manual(self):
        out = self.run_install("pacman", ["aur:thing-git"])
        self.assertIn("no AUR helper", out)
        self.assertIn("thing-git", out)
        self.assertNotIn("$ ", out, "must not invent a command it can't run")

    def test_aur_entries_dont_leak_into_plain_install(self):
        out = self.run_install("pacman", ["git", "aur:thing-git"], helpers=("yay",))
        self.assertIn("sudo pacman -S --needed --noconfirm git", out)
        self.assertNotIn("--noconfirm git aur:thing-git", out)


class TestLanguageInstallCommands(unittest.TestCase):
    def run_install(self, lang, spec):
        buf = io.StringIO()
        with redirect_stdout(buf):
            sync.install_via_lang(lang, spec, dry_run=True)
        return buf.getvalue()

    def test_cargo_pinned(self):
        self.assertIn("cargo install eza --version 0.18.0",
                      self.run_install("cargo", "eza@0.18.0"))

    def test_cargo_unpinned(self):
        self.assertIn("cargo install eza", self.run_install("cargo", "eza"))

    def test_go_adds_latest_when_unpinned(self):
        self.assertIn("go install example.com/x@latest",
                      self.run_install("go", "example.com/x"))

    def test_go_keeps_explicit_version(self):
        self.assertIn("go install example.com/x@v1.2.3",
                      self.run_install("go", "example.com/x@v1.2.3"))

    def test_uv_pinned(self):
        self.assertIn("uv tool install black==24.1.0",
                      self.run_install("uv", "black@24.1.0"))


class TestReleaseUrl(unittest.TestCase):
    SPEC = {
        "url": "https://x/v{version}/tool-{version}.{os}.{arch}.tar.gz",
        "version": "1.2.3",
        "bin": "tool-{version}/tool",
    }

    def resolve(self, spec, system="Linux", machine="x86_64"):
        with mock.patch.object(sync.platform, "system", lambda: system), \
             mock.patch.object(sync.platform, "machine", lambda: machine):
            return sync.release_url_and_bin("tool", spec)

    def test_linux_x86_64(self):
        url, binpath = self.resolve(self.SPEC)
        self.assertEqual(url, "https://x/v1.2.3/tool-1.2.3.linux.x86_64.tar.gz")
        self.assertEqual(binpath, "tool-1.2.3/tool")

    def test_macos_arm(self):
        url, _ = self.resolve(self.SPEC, system="Darwin", machine="arm64")
        self.assertEqual(url, "https://x/v1.2.3/tool-1.2.3.darwin.aarch64.tar.gz")

    def test_amd64_normalized(self):
        url, _ = self.resolve(self.SPEC, machine="amd64")
        self.assertIn("x86_64", url)

    def test_custom_maps(self):
        spec = dict(self.SPEC, os_map={"linux": "unknown-linux-gnu"},
                    arch_map={"x86_64": "amd64"})
        url, _ = self.resolve(spec)
        self.assertEqual(url, "https://x/v1.2.3/tool-1.2.3.unknown-linux-gnu.amd64.tar.gz")

    def test_unmapped_platform_passes_through(self):
        url, _ = self.resolve(self.SPEC, machine="riscv64")
        self.assertIn("riscv64", url)

    def test_bin_optional(self):
        _, binpath = self.resolve({"url": "https://x/raw", "version": "1"})
        self.assertIsNone(binpath)


class TestSafeExtract(unittest.TestCase):
    """The one security-relevant path: archives must not escape their directory."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pc-extract-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def assertRejects(self, archive_path, opener):
        dest = self.tmp / "out"
        with opener(archive_path) as archive:
            with self.assertRaises(SystemExit) as ctx:
                sync.safe_extract(archive, dest)
        self.assertIn("unsafe path", str(ctx.exception))

    def test_tar_rejects_traversal(self):
        p = make_malicious_tar(self.tmp / "bad.tar", "../escaped")
        self.assertRejects(p, tarfile.open)
        self.assertFalse((self.tmp / "escaped").exists())

    def test_tar_rejects_absolute(self):
        p = make_malicious_tar(self.tmp / "abs.tar", "/tmp/pc-escaped")
        self.assertRejects(p, tarfile.open)

    def test_tar_rejects_nested_traversal(self):
        p = make_malicious_tar(self.tmp / "nested.tar", "a/b/../../../escaped")
        self.assertRejects(p, tarfile.open)

    def test_zip_rejects_traversal(self):
        p = make_malicious_zip(self.tmp / "bad.zip", "../escaped")
        self.assertRejects(p, zipfile.ZipFile)

    def test_zip_rejects_absolute(self):
        p = make_malicious_zip(self.tmp / "abs.zip", "/tmp/pc-escaped")
        self.assertRejects(p, zipfile.ZipFile)

    def test_safe_archive_extracts(self):
        path = self.tmp / "good.tar"
        with tarfile.open(path, "w") as tf:
            data = b"fine\n"
            info = tarfile.TarInfo("dir/file")
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
        dest = self.tmp / "out"
        with tarfile.open(path) as tf:
            sync.safe_extract(tf, dest)
        self.assertEqual((dest / "dir" / "file").read_bytes(), b"fine\n")


class TestModuleIgnore(unittest.TestCase):
    def module(self, ignore):
        return sync.Module("m", Path("/nonexistent"),
                           {"destination": "~/", "ignore": ignore})

    def test_bare_filename_pattern_matches_at_any_depth(self):
        mod = self.module([".netrwhist"])
        self.assertTrue(mod.is_ignored(Path(".netrwhist")))
        self.assertTrue(mod.is_ignored(Path("nested/deep/.netrwhist")))

    def test_glob_on_relative_path(self):
        mod = self.module(["cache/*"])
        self.assertTrue(mod.is_ignored(Path("cache/x.txt")))
        self.assertFalse(mod.is_ignored(Path("keep/x.txt")))

    def test_extension_glob(self):
        mod = self.module(["*.log"])
        self.assertTrue(mod.is_ignored(Path("a/b.log")))
        self.assertFalse(mod.is_ignored(Path("a/b.txt")))

    def test_empty_ignore_matches_nothing(self):
        self.assertFalse(self.module([]).is_ignored(Path("anything")))


class TestPackageState(unittest.TestCase):
    """Release detection must not depend on PATH -- a fresh machine won't have
    ~/.local/bin set up yet, but the binary is still installed."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pc-state-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        self.receipts = self.tmp / "receipts.json"
        patches = [
            mock.patch.object(sync, "LOCAL_BIN", self.bin),
            mock.patch.object(sync, "RECEIPTS_FILE", self.receipts),
            # PATH deliberately empty: detection must not consult it.
            mock.patch.object(sync.shutil, "which", lambda t: None),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def state(self, version="1.0.0", entry=None):
        return sync.package_state("toy", entry or {}, "release", {"version": version})

    def test_missing_when_absent(self):
        self.assertEqual(self.state(), "missing")

    def test_installed_when_present_despite_empty_path(self):
        (self.bin / "toy").write_text("")
        self.receipts.write_text(json.dumps({"toy": {"method": "release",
                                                     "version": "1.0.0"}}))
        self.assertEqual(self.state(), "installed")

    def test_outdated_when_pin_moves(self):
        (self.bin / "toy").write_text("")
        self.receipts.write_text(json.dumps({"toy": {"method": "release",
                                                     "version": "1.0.0"}}))
        self.assertEqual(self.state(version="2.0.0"), "outdated (1.0.0 -> 2.0.0)")

    def test_bin_override_respected(self):
        (self.bin / "toybin").write_text("")
        self.assertEqual(self.state(entry={"bin": "toybin"}), "installed")


if __name__ == "__main__":
    unittest.main()
