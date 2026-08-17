"""Tier 1a: in-process tests of sync.py helpers.

Covers logic the CLI can't reach on a given machine -- you can't run pacman or
brew on a Debian box, but their command construction still has to be right.
"""

import base64
import io
import json
import shutil
import subprocess
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
            return sync.resolve(entry, sysmgr)

    def test_manager_names_the_package(self):
        self.assertEqual(self.resolve({"apt": "thing-bin"}), [("apt", "thing-bin")])

    def test_omitted_manager_is_skipped(self):
        """Names are never guessed from the entry key -- that's the collision."""
        self.assertEqual(self.resolve({}), [])

    def test_null_skips_manager(self):
        """Explicit null reads as 'deliberately unavailable'; same effect."""
        self.assertEqual(self.resolve({"apt": None}), [])

    def test_other_managers_dont_leak_in(self):
        self.assertEqual(self.resolve({"pacman": "thing"}), [])

    def test_no_system_manager_detected(self):
        self.assertEqual(self.resolve({"apt": "thing"}, sysmgr=None), [])

    def test_language_installer_requires_toolchain(self):
        entry = {"apt": None, "cargo": "thing@1.0"}
        self.assertEqual(self.resolve(entry), [], "cargo absent: must not be offered")
        self.assertEqual(self.resolve(entry, have=("cargo",)), [("cargo", "thing@1.0")])

    def test_method_ordering(self):
        entry = {"apt": "thing", "cargo": "thing",
                 "release": {"version": "1"}, "build": {"git": "u"}}
        methods = [m for m, _ in self.resolve(entry, have=("cargo",))]
        self.assertEqual(methods, ["apt", "cargo", "release", "build"])

    def test_prefer_overrides_order(self):
        entry = {"apt": "thing", "cargo": "thing",
                 "release": {"version": "1"}, "prefer": "release"}
        methods = [m for m, _ in self.resolve(entry, have=("cargo",))]
        self.assertEqual(methods[0], "release")

    def test_prefer_keeps_others_as_fallback(self):
        entry = {"apt": "thing", "release": {"version": "1"}, "prefer": "release"}
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


class RepoCase(unittest.TestCase):
    """Relocates the /etc paths so nothing here can reach the real system."""

    SPEC = {"uris": "https://x/packages", "suites": "stable", "components": "main",
            "key": "keys/toy.asc", "key_url": "https://x/key.gpg"}

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pc-repo-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.sysroot = self.tmp / "sysroot"
        self.repo = self.tmp / "repo"
        (self.repo / "keys").mkdir(parents=True)
        (self.repo / "keys" / "toy.asc").write_text("KEYBYTES\n")
        patches = [
            mock.patch.object(sync, "SYSROOT", self.sysroot),
            mock.patch.object(sync, "REPO", self.repo),
            mock.patch.object(sync, "KEYS_DIR", self.repo / "keys"),
            mock.patch.object(sync, "APT_SOURCES_DIR",
                              self.sysroot / "etc/apt/sources.list.d"),
            mock.patch.object(sync, "APT_KEYRINGS_DIR", self.sysroot / "etc/apt/keyrings"),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def capture(self, fn, *args, **kwargs):
        buf = io.StringIO()
        with redirect_stdout(buf):
            result = fn(*args, **kwargs)
        return result, buf.getvalue()


class TestRenderAptSource(RepoCase):
    def test_exact_output(self):
        self.assertEqual(sync.render_apt_source("toy", self.SPEC), """\
# Managed by personal-config (repo: toy). Local edits are overwritten.
Types: deb
URIs: https://x/packages
Suites: stable
Components: main
Signed-By: /etc/apt/keyrings/toy.asc
""")

    def test_signed_by_ignores_the_sysroot(self):
        """A file rendered under test must be byte-identical to a real one, or
        drift detection compares against the wrong thing."""
        self.assertIn("Signed-By: /etc/apt/keyrings/toy.asc",
                      sync.render_apt_source("toy", self.SPEC))
        self.assertNotIn(str(self.sysroot), sync.render_apt_source("toy", self.SPEC))

    def test_types_can_be_overridden(self):
        spec = dict(self.SPEC, types="deb deb-src")
        self.assertIn("Types: deb deb-src", sync.render_apt_source("toy", spec))

    def test_architectures_omitted_by_default(self):
        self.assertNotIn("Architectures", sync.render_apt_source("toy", self.SPEC))

    def test_architectures_literal_passes_through(self):
        spec = dict(self.SPEC, architectures="amd64 arm64")
        self.assertIn("Architectures: amd64 arm64", sync.render_apt_source("toy", spec))

    def test_suites_auto_reads_os_release(self):
        (self.sysroot / "etc").mkdir(parents=True)
        (self.sysroot / "etc/os-release").write_text('ID=ubuntu\nVERSION_CODENAME="noble"\n')
        spec = dict(self.SPEC, suites="auto")
        self.assertIn("Suites: noble", sync.render_apt_source("toy", spec))

    def test_architectures_auto_reads_dpkg(self):
        spec = dict(self.SPEC, architectures="auto")
        with mock.patch.object(sync, "dpkg_architecture", lambda: "arm64"):
            self.assertIn("Architectures: arm64", sync.render_apt_source("toy", spec))

    def test_no_templating_on_values(self):
        """dnf will need $releasever to survive this path untouched."""
        spec = dict(self.SPEC, uris="https://x/$releasever/{version}")
        self.assertIn("URIs: https://x/$releasever/{version}",
                      sync.render_apt_source("toy", spec))

    def test_newline_in_value_is_rejected(self):
        spec = dict(self.SPEC, suites="stable\nURIs: https://evil")
        with self.assertRaises(SystemExit):
            sync.render_apt_source("toy", spec)

    def test_auto_unsupported_for_other_fields(self):
        spec = dict(self.SPEC, components="auto")
        with self.assertRaises(SystemExit):
            sync.render_apt_source("toy", spec)


class TestRepoNaming(RepoCase):
    def test_name_defaults_to_the_package(self):
        self.assertEqual(sync.apt_repo_name("github-cli", self.SPEC), "github-cli")

    def test_name_override(self):
        spec = dict(self.SPEC, name="docker")
        self.assertEqual(sync.apt_repo_name("docker-ce", spec), "docker")

    def test_keyring_suffix_follows_the_repo_copy(self):
        self.assertEqual(sync.apt_key_dest("toy", self.SPEC).name, "toy.asc")

    def test_key_must_stay_inside_the_repo(self):
        for bad in ("../../etc/passwd", "/etc/passwd"):
            with self.subTest(key=bad):
                with self.assertRaises(SystemExit):
                    sync.key_file("toy", dict(self.SPEC, key=bad))

    def test_missing_key_names_the_fetch_command(self):
        with self.assertRaises(SystemExit) as ctx:
            sync.key_file("toy", dict(self.SPEC, key="keys/absent.asc"))
        self.assertIn("fetch-key toy", str(ctx.exception))


class TestRepoState(RepoCase):
    def install(self, dry_run=False):
        changed, _out = self.capture(sync.install_apt_repo, "toy", self.SPEC, dry_run)
        return changed

    def test_missing_then_drifted_then_present(self):
        self.assertEqual(sync.repo_state("toy", self.SPEC), "missing")
        self.install()
        self.assertEqual(sync.repo_state("toy", self.SPEC), "present")
        source = sync.apt_source_dest("toy")
        source.write_text(source.read_text() + "junk\n")
        self.assertEqual(sync.repo_state("toy", self.SPEC), "drifted")

    def test_drifted_when_only_the_key_changes(self):
        self.install()
        (self.repo / "keys" / "toy.asc").write_text("ROTATED\n")
        self.assertEqual(sync.repo_state("toy", self.SPEC), "drifted")

    def test_install_is_idempotent(self):
        self.assertTrue(self.install())
        self.assertFalse(self.install())

    def test_dry_run_writes_nothing(self):
        _, out = self.capture(sync.install_apt_repo, "toy", self.SPEC, dry_run=True)
        self.assertIn("install -m 0644", out)
        self.assertFalse(self.sysroot.exists())


class TestRepoSpecs(RepoCase):
    def entry(self, **kw):
        return dict({"apt": "toy", "repo": {"apt": self.SPEC}}, **kw)

    def test_inert_for_another_manager(self):
        self.assertEqual(sync.repo_specs({"toy": self.entry()}, "pacman"), {})

    def test_inert_when_the_package_comes_from_elsewhere(self):
        """A tool installed via cargo here must not drag its apt repo along."""
        entry = {"apt": "toy", "cargo": "toy", "prefer": "cargo",
                 "repo": {"apt": self.SPEC}}
        with mock.patch.object(sync.shutil, "which", lambda t: t):
            self.assertEqual(sync.repo_specs({"toy": entry}, "apt"), {})

    def test_shared_name_collapses(self):
        spec = dict(self.SPEC, name="shared")
        packages = {"a": {"apt": "a", "repo": {"apt": spec}},
                    "b": {"apt": "b", "repo": {"apt": spec}}}
        self.assertEqual(list(sync.repo_specs(packages, "apt")), ["shared"])

    def test_conflicting_definitions_are_fatal(self):
        packages = {"a": {"apt": "a", "repo": {"apt": dict(self.SPEC, name="shared")}},
                    "b": {"apt": "b", "repo": {"apt": dict(self.SPEC, name="shared",
                                                           suites="beta")}}}
        with self.assertRaises(SystemExit) as ctx:
            sync.repo_specs(packages, "apt")
        self.assertIn("shared", str(ctx.exception))


class TestPrivilegedWrite(RepoCase):
    def commands(self, root=False, real_sysroot=False):
        stack = [mock.patch.object(sync.os, "geteuid", lambda: 0 if root else 1000)]
        if real_sysroot:
            stack.append(mock.patch.object(sync, "SYSROOT", Path("/")))
        buf = io.StringIO()
        for p in stack:
            p.start()
        try:
            with redirect_stdout(buf):
                sync.write_privileged(self.sysroot / "etc/x", b"data", dry_run=True)
        finally:
            for p in stack:
                p.stop()
        return buf.getvalue()

    def test_test_sysroot_needs_no_sudo(self):
        out = self.commands()
        self.assertIn("install -m 0644", out)
        self.assertNotIn("sudo", out)

    def test_real_sysroot_as_user_uses_sudo(self):
        self.assertIn("sudo install -m 0644", self.commands(real_sysroot=True))

    def test_root_drops_sudo(self):
        self.assertNotIn("sudo", self.commands(root=True, real_sysroot=True))

    def test_directory_created_only_when_absent(self):
        self.assertIn("install -d -m 0755", self.commands())
        (self.sysroot / "etc").mkdir(parents=True)
        self.assertNotIn("install -d", self.commands())

    def test_unchanged_content_is_silent(self):
        dest = self.sysroot / "etc/x"
        dest.parent.mkdir(parents=True)
        dest.write_bytes(b"data")
        changed, out = self.capture(sync.write_privileged, dest, b"data", dry_run=False)
        self.assertFalse(changed)
        self.assertEqual(out, "")


class TestRefresh(RepoCase):
    def test_skipped_under_a_test_sysroot(self):
        """Without this, tier 1 would run a real apt-get update on the machine."""
        _, out = self.capture(sync.refresh_manager, "apt", dry_run=False)
        self.assertIn("skipping apt-get update", out)

    def test_uses_apt_get_under_sudo(self):
        with mock.patch.object(sync, "SYSROOT", Path("/")), \
             mock.patch.object(sync.os, "geteuid", lambda: 1000):
            _, out = self.capture(sync.refresh_manager, "apt", dry_run=True)
        self.assertIn("sudo apt-get update", out)

    def test_unsupported_manager_is_a_no_op(self):
        _, out = self.capture(sync.refresh_manager, "brew", dry_run=True)
        self.assertEqual(out, "")

    def test_failure_exits_with_a_repair_hint(self):
        boom = subprocess.CalledProcessError(1, "apt-get")
        with mock.patch.object(sync, "SYSROOT", Path("/")), \
             mock.patch.object(sync, "run_or_print", mock.Mock(side_effect=boom)):
            with self.assertRaises(SystemExit) as ctx:
                sync.refresh_manager("apt", dry_run=False)
        self.assertIn("sources.list.d", str(ctx.exception))


class TestEnarmor(unittest.TestCase):
    """Vendors mostly serve binary keys; armoring keeps the repo reviewable text."""

    KEY = bytes(bytearray([0x99, 0x02, 0x0D, 0x04]) + bytearray(range(256)) * 3)

    def test_round_trips(self):
        text = sync.enarmor(self.KEY)
        body = "".join(line for line in text.splitlines()[2:]
                       if line and not line.startswith(("=", "-")))
        self.assertEqual(base64.b64decode(body), self.KEY)

    def test_shape(self):
        text = sync.enarmor(self.KEY)
        self.assertTrue(text.startswith("-----BEGIN PGP PUBLIC KEY BLOCK-----\n\n"))
        self.assertTrue(text.endswith("-----END PGP PUBLIC KEY BLOCK-----\n"))
        lines = text.splitlines()
        self.assertTrue(all(len(line) <= 64 for line in lines[2:-2]))
        self.assertRegex(lines[-2], r"^=[A-Za-z0-9+/]{4}$")

    def test_known_crc24_vector(self):
        """gpg --enarmor of an empty body; guards the CRC24 loop."""
        self.assertIn("=twTO", sync.enarmor(b""))

    def test_matches_the_committed_github_key(self):
        path = Path(sync.REPO) / "keys" / "github-cli.asc"
        if not path.is_file():
            self.skipTest("keys/github-cli.asc not present")
        text = path.read_text()
        body = "".join(line for line in text.splitlines()[2:]
                       if line and not line.startswith(("=", "-")))
        self.assertEqual(sync.enarmor(base64.b64decode(body)), text)


if __name__ == "__main__":
    unittest.main()
