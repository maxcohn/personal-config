"""Tier 1b: sync.py driven as a black box, with HOME and the repo in tempdirs.

These run the real CLI in a subprocess, so they test what actually ships --
including the module-level path constants that in-process patching would bypass.
"""

import json
import os
import stat
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import (Sandbox, FileServer, make_toy_tarball, make_toy_zip,  # noqa: E402
                     make_raw_binary)


class TestDeploy(Sandbox):
    def setUp(self):
        super().setUp()
        self.module("vimlike", "~/.vim/", {
            "vimrc": "set number\n",
            "colors/theme.vim": "\" theme\n",
            "spell/en.add": "word\n",
        })
        self.module("shell", "~/", {".shrc": "alias x=y\n"})

    def test_deploy_creates_expected_layout(self):
        self.sync("deploy", expect=0)
        self.assertEqual((self.home / ".vim/vimrc").read_text(), "set number\n")
        self.assertEqual((self.home / ".vim/colors/theme.vim").read_text(), "\" theme\n")
        self.assertEqual((self.home / ".shrc").read_text(), "alias x=y\n")

    def test_deploy_is_idempotent(self):
        self.sync("deploy", expect=0)
        after_first = self.snapshot(self.home)
        second = self.sync("deploy", expect=0)
        self.assertEqual(second.stdout.strip(), "",
                         "second deploy should report no work")
        self.assertUnchanged(after_first, self.home)

    def test_deploy_dry_run_changes_nothing(self):
        before = self.snapshot(self.home)
        proc = self.sync("deploy", "--dry-run", expect=0)
        self.assertIn("vimrc", proc.stdout)
        self.assertUnchanged(before, self.home)

    def test_deploy_single_module(self):
        self.sync("deploy", "shell", expect=0)
        self.assertTrue((self.home / ".shrc").exists())
        self.assertFalse((self.home / ".vim").exists())

    def test_deploy_overwrites_divergent_file(self):
        self.sync("deploy", expect=0)
        (self.home / ".shrc").write_text("local edit\n")
        self.sync("deploy", expect=0)
        self.assertEqual((self.home / ".shrc").read_text(), "alias x=y\n")

    def test_unknown_module_errors_helpfully(self):
        proc = self.sync("deploy", "nope", expect=1)
        self.assertIn("unknown module", proc.stderr)
        self.assertIn("shell", proc.stderr, "should list what's available")


class TestStatusAndDiff(Sandbox):
    def setUp(self):
        super().setUp()
        self.module("shell", "~/", {".shrc": "one\ntwo\n"})

    def test_clean_exits_zero(self):
        self.sync("deploy", expect=0)
        proc = self.sync("status", expect=0)
        self.assertIn("ok", proc.stdout)

    def test_modified_exits_nonzero(self):
        self.sync("deploy", expect=0)
        (self.home / ".shrc").write_text("one\nCHANGED\n")
        proc = self.sync("status", expect=1)
        self.assertIn("modified", proc.stdout)

    def test_missing_reported(self):
        proc = self.sync("status", expect=1)
        self.assertIn("missing", proc.stdout)

    def test_diff_shows_unified_diff(self):
        self.sync("deploy", expect=0)
        (self.home / ".shrc").write_text("one\nCHANGED\n")
        proc = self.sync("diff", expect=0)
        self.assertIn("-two", proc.stdout)
        self.assertIn("+CHANGED", proc.stdout)
        self.assertIn("@@", proc.stdout)

    def test_diff_on_missing_file_does_not_crash(self):
        proc = self.sync("diff", expect=0)
        self.assertIn("missing on system", proc.stdout)

    def test_binary_files_reported_not_crashed(self):
        self.module("bin", "~/bin/", {"blob": b"\x00\x01\x02"})
        self.sync("deploy", expect=0)
        (self.home / "bin/blob").write_bytes(b"\x00\xff\x02")
        proc = self.sync("diff", "bin", expect=0)
        self.assertIn("Binary files", proc.stdout)


class TestCapture(Sandbox):
    def setUp(self):
        super().setUp()
        self.module("shell", "~/", {".shrc": "original\n"}, ignore=["*.log"])

    def test_capture_pulls_back_changes(self):
        self.sync("deploy", expect=0)
        (self.home / ".shrc").write_text("edited on machine\n")
        self.sync("capture", expect=0)
        self.assertEqual((self.modules / "shell/.shrc").read_text(),
                         "edited on machine\n")

    def test_capture_dry_run_changes_nothing(self):
        self.sync("deploy", expect=0)
        (self.home / ".shrc").write_text("edited\n")
        before = self.snapshot(self.repo)
        proc = self.sync("capture", "--dry-run", expect=0)
        self.assertIn(".shrc", proc.stdout)
        self.assertUnchanged(before, self.repo)

    def test_capture_does_not_adopt_untracked_files(self):
        """The repo defines what's managed; junk in the destination stays out."""
        self.sync("deploy", expect=0)
        (self.home / "plugin-junk.vim").write_text("junk\n")
        (self.home / "history").write_text("secrets\n")
        self.sync("capture", expect=0)
        self.assertFalse((self.modules / "shell/plugin-junk.vim").exists())
        self.assertFalse((self.modules / "shell/history").exists())

    def test_capture_respects_ignore(self):
        self.write(self.modules / "shell/debug.log", "repo\n")
        self.write(self.home / "debug.log", "machine\n")
        self.sync("capture", expect=0)
        self.assertEqual((self.modules / "shell/debug.log").read_text(), "repo\n")

    def test_ignored_file_not_deployed(self):
        self.write(self.modules / "shell/debug.log", "repo\n")
        self.sync("deploy", expect=0)
        self.assertFalse((self.home / "debug.log").exists())

    def test_capture_skips_absent_destination(self):
        proc = self.sync("capture", expect=0)
        self.assertIn("missing on system", proc.stdout)
        self.assertEqual((self.modules / "shell/.shrc").read_text(), "original\n")

    def test_manifest_never_deployed(self):
        self.sync("deploy", expect=0)
        self.assertFalse((self.home / "manifest.json").exists())


class TestPackagesDryRun(Sandbox):
    def setUp(self):
        super().setUp()
        self.module("shell", "~/", {".shrc": "x\n"})

    def test_status_reports_resolution(self):
        self.packages({"definitely-not-a-real-package-xyz":
                       {"apt": "definitely-not-a-real-package-xyz"}})
        proc = self.sync("packages", "status", "--manager", "apt", expect=0)
        self.assertIn("missing", proc.stdout)
        self.assertIn("via apt", proc.stdout)

    def test_install_dry_run_runs_nothing(self):
        self.packages({"definitely-not-a-real-package-xyz": {"apt": "fake-pkg"}})
        proc = self.sync("packages", "install", "--dry-run", "--manager", "apt",
                         expect=0)
        self.assertIn("apt install -y fake-pkg", proc.stdout)

    def test_build_entry_reports_not_implemented(self):
        self.packages({"neovim": {"apt": None,
                                  "build": {"git": "https://x", "ref": "v1"}}})
        proc = self.sync("packages", "status", "--manager", "apt", expect=0)
        self.assertIn("not yet implemented", proc.stdout)

    def test_build_entry_install_does_not_crash(self):
        self.packages({"neovim": {"apt": None, "build": {"git": "https://x"}}})
        proc = self.sync("packages", "install", "--manager", "apt", expect=0)
        self.assertIn("nothing to install", proc.stdout)

    def test_skipped_when_no_method_available(self):
        self.packages({"xclip": {"apt": None}})
        proc = self.sync("packages", "status", "--manager", "apt", expect=0)
        self.assertIn("skipped", proc.stdout)

    def test_entry_naming_no_method_is_skipped(self):
        """Nothing is inferred from the entry key, so this can't install."""
        self.packages({"ripgrep": {}})
        proc = self.sync("packages", "status", "--manager", "apt", expect=0)
        self.assertIn("skipped", proc.stdout)

    def test_unknown_manager_errors(self):
        self.packages({"git": {"apt": "git"}})
        proc = self.sync("packages", "status", "--manager", "nonesuch", expect=1)
        self.assertIn("not found on PATH", proc.stderr)

    def test_path_warning_when_local_bin_absent(self):
        self.packages({"git": {"apt": "git"}})
        proc = self.sync("packages", "status", "--manager", "apt", path="/usr/bin:/bin")
        self.assertIn("not on PATH", proc.stdout)

    def test_path_warning_suppressed_when_present(self):
        self.packages({"git": {"apt": "git"}})
        proc = self.sync("packages", "status", "--manager", "apt",
                         path="{}/.local/bin:/usr/bin:/bin".format(self.home))
        self.assertNotIn("not on PATH", proc.stdout)


class TestReleaseInstall(Sandbox):
    """Exercises real download + extract + chmod + receipt against a local server."""

    def setUp(self):
        super().setUp()
        self.module("shell", "~/", {".shrc": "x\n"})
        self.served = self.tmp / "served"
        self.served.mkdir()
        self.server = FileServer(self.served)
        self.server.__enter__()
        self.addCleanup(self.server.__exit__)

    def declare(self, filename, version, bin_in_archive="toy-{version}/toy"):
        spec = {
            "url": "{}/{}".format(self.server.url, filename),
            "version": version,
        }
        if bin_in_archive:
            spec["bin"] = bin_in_archive
        self.packages({"toy": {"apt": None, "release": spec}})

    @property
    def installed_bin(self):
        return self.home / ".local/bin/toy"

    @property
    def receipts(self):
        return json.loads(
            (self.home / ".local/state/personal-config/installed.json").read_text())

    def install(self):
        return self.sync("packages", "install", "--manager", "apt", expect=0)

    def test_tarball_install_end_to_end(self):
        make_toy_tarball(self.served / "toy-1.0.0.tar.gz", "1.0.0")
        self.declare("toy-1.0.0.tar.gz", "1.0.0")
        self.install()
        self.assertTrue(self.installed_bin.is_file())
        self.assertTrue(os.access(str(self.installed_bin), os.X_OK),
                        "installed binary must be executable")
        out = subprocess.run([str(self.installed_bin)], capture_output=True, text=True)
        self.assertEqual(out.stdout.strip(), "toy 1.0.0")
        self.assertEqual(self.receipts["toy"],
                         {"method": "release", "version": "1.0.0"})

    def test_xz_tarball(self):
        make_toy_tarball(self.served / "toy-1.0.0.tar.xz", "1.0.0", compression="xz")
        self.declare("toy-1.0.0.tar.xz", "1.0.0")
        self.install()
        self.assertTrue(self.installed_bin.is_file())

    def test_zip_archive(self):
        make_toy_zip(self.served / "toy-1.0.0.zip", "1.0.0")
        self.declare("toy-1.0.0.zip", "1.0.0")
        self.install()
        self.assertTrue(os.access(str(self.installed_bin), os.X_OK))

    def test_raw_binary(self):
        make_raw_binary(self.served / "toy", "1.0.0")
        self.declare("toy", "1.0.0", bin_in_archive=None)
        self.install()
        out = subprocess.run([str(self.installed_bin)], capture_output=True, text=True)
        self.assertEqual(out.stdout.strip(), "toy 1.0.0")

    def test_status_installed_after_install(self):
        make_toy_tarball(self.served / "toy-1.0.0.tar.gz", "1.0.0")
        self.declare("toy-1.0.0.tar.gz", "1.0.0")
        self.install()
        proc = self.sync("packages", "status", "--manager", "apt", expect=0)
        self.assertIn("installed", proc.stdout)
        self.assertNotIn("missing", proc.stdout)

    def test_reinstall_is_noop(self):
        make_toy_tarball(self.served / "toy-1.0.0.tar.gz", "1.0.0")
        self.declare("toy-1.0.0.tar.gz", "1.0.0")
        self.install()
        proc = self.install()
        self.assertIn("nothing to install", proc.stdout)

    def test_version_bump_reports_outdated_then_upgrades(self):
        make_toy_tarball(self.served / "toy-1.0.0.tar.gz", "1.0.0")
        self.declare("toy-1.0.0.tar.gz", "1.0.0")
        self.install()

        make_toy_tarball(self.served / "toy-2.0.0.tar.gz", "2.0.0")
        self.declare("toy-2.0.0.tar.gz", "2.0.0")
        proc = self.sync("packages", "status", "--manager", "apt", expect=0)
        self.assertIn("outdated (1.0.0 -> 2.0.0)", proc.stdout)

        self.install()
        out = subprocess.run([str(self.installed_bin)], capture_output=True, text=True)
        self.assertEqual(out.stdout.strip(), "toy 2.0.0")
        self.assertEqual(self.receipts["toy"]["version"], "2.0.0")

    def test_dry_run_downloads_nothing(self):
        make_toy_tarball(self.served / "toy-1.0.0.tar.gz", "1.0.0")
        self.declare("toy-1.0.0.tar.gz", "1.0.0")
        proc = self.sync("packages", "install", "--dry-run", "--manager", "apt",
                         expect=0)
        self.assertIn("fetch", proc.stdout)
        self.assertFalse(self.installed_bin.exists())

    def test_bin_override_names_installed_file(self):
        make_toy_tarball(self.served / "toy-1.0.0.tar.gz", "1.0.0")
        self.packages({"toy": {
            "apt": None,
            "bin": "toytool",
            "release": {"url": "{}/toy-1.0.0.tar.gz".format(self.server.url),
                        "version": "1.0.0", "bin": "toy-1.0.0/toy"},
        }})
        self.install()
        self.assertTrue((self.home / ".local/bin/toytool").is_file())


if __name__ == "__main__":
    unittest.main()
