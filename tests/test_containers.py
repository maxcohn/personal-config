"""Tier 2: run sync.py inside throwaway containers.

These prove the things a single dev machine can't: that pacman/dnf paths really
work, that a genuinely clean HOME gets bootstrapped correctly, and that a system
with no supported package manager degrades gracefully instead of crashing.

The repo is mounted read-only and copied inside, so a buggy capture can never
touch real files. Everything runs as root in-container, which also exercises the
no-sudo branch of sudo_prefix().

Skipped entirely when no container runtime is available.
"""

import os
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from helpers import REPO_ROOT, container_runtime  # noqa: E402

CONTAINERS_DIR = Path(__file__).resolve().parent / "containers"
RUNTIME = container_runtime()
REBUILD = os.environ.get("PC_TEST_REBUILD") == "1"
ONLY_DISTRO = os.environ.get("PC_TEST_DISTRO")

# Per-distro: a package guaranteed present in the base image, and one to install.
DISTROS = {
    "debian": {"preinstalled": "python3", "installable": "tree"},
    "arch": {"preinstalled": "python", "installable": "tree"},
    "fedora": {"preinstalled": "python3", "installable": "tree"},
    "alpine": {"preinstalled": None, "installable": None},  # no supported manager
}

_built = set()


def image_for(distro):
    return "pc-test-{}".format(distro)


def build_image(distro):
    """Build once per process; images cache between runs unless PC_TEST_REBUILD=1."""
    tag = image_for(distro)
    if tag in _built:
        return
    exists = subprocess.run([RUNTIME, "image", "exists", tag],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if exists.returncode != 0 or REBUILD:
        proc = subprocess.run(
            [RUNTIME, "build", "-t", tag,
             "-f", str(CONTAINERS_DIR / "Dockerfile.{}".format(distro)),
             str(CONTAINERS_DIR)],
            capture_output=True, text=True)
        if proc.returncode != 0:
            raise unittest.SkipTest(
                "could not build {} image:\n{}".format(tag, proc.stderr[-2000:]))
    _built.add(tag)


# Copies the repo out of the read-only mount and points HOME at a clean dir.
PRELUDE = """
set -e
cp -r /src /repo
rm -rf /repo/.git
export HOME=/testhome
mkdir -p $HOME
cd /repo
"""


class ContainerCase(unittest.TestCase):
    distro = None

    @classmethod
    def setUpClass(cls):
        if RUNTIME is None:
            raise unittest.SkipTest(
                "no usable container runtime (podman or docker); "
                "tier 2 skipped -- run ./test.py without --containers for tier 1")
        if ONLY_DISTRO and ONLY_DISTRO != cls.distro:
            raise unittest.SkipTest("filtered out by --distro {}".format(ONLY_DISTRO))
        build_image(cls.distro)

    def run_in(self, script, expect=0):
        proc = subprocess.run(
            [RUNTIME, "run", "--rm",
             "-v", "{}:/src:ro".format(REPO_ROOT),
             image_for(self.distro), "sh", "-c", PRELUDE + script],
            capture_output=True, text=True)
        if expect is not None:
            self.assertEqual(
                proc.returncode, expect,
                "expected exit {}, got {}\nstdout:\n{}\nstderr:\n{}".format(
                    expect, proc.returncode, proc.stdout, proc.stderr))
        return proc

    # -- shared scenarios ---------------------------------------------------

    def test_deploy_onto_clean_home(self):
        proc = self.run_in("""
            python3 sync.py deploy
            test -f $HOME/.vim/vimrc || { echo MISSING_VIMRC; exit 1; }
            test -f $HOME/.vim/colors/onedark.vim || { echo MISSING_COLORS; exit 1; }
            test -f $HOME/.zshrc || { echo MISSING_ZSHRC; exit 1; }
            test -f $HOME/.zaliases || { echo MISSING_ALIASES; exit 1; }
            test -f $HOME/.vim/manifest.json && { echo MANIFEST_LEAKED; exit 1; }
            python3 sync.py status
            echo DEPLOY_OK
        """)
        self.assertIn("DEPLOY_OK", proc.stdout)

    def test_status_clean_after_deploy_then_detects_drift(self):
        proc = self.run_in("""
            python3 sync.py deploy >/dev/null
            python3 sync.py status || { echo UNEXPECTED_DRIFT; exit 1; }
            echo "drifted" >> $HOME/.zshrc
            if python3 sync.py status; then echo DRIFT_NOT_DETECTED; exit 1; fi
            python3 sync.py capture
            python3 sync.py status || { echo STILL_DIRTY; exit 1; }
            echo DRIFT_OK
        """)
        self.assertIn("DRIFT_OK", proc.stdout)
        self.assertIn("modified", proc.stdout)


class ManagedDistroCase(ContainerCase):
    """Distros with a supported package manager."""

    def fixture(self, extra=""):
        info = DISTROS[self.distro]
        return """
            cat > /repo/packages.json <<'EOF'
{{
	"{pre}": {{}},
	"{inst}": {{}},
	"definitely-not-real-xyz": {{}}
}}
EOF
        """.format(pre=info["preinstalled"], inst=info["installable"]) + extra

    def test_detects_preinstalled_and_missing(self):
        info = DISTROS[self.distro]
        proc = self.run_in(self.fixture("python3 sync.py packages status"))
        lines = {line.split()[0]: line for line in proc.stdout.splitlines()
                 if line.startswith("  ")}
        self.assertIn("installed", lines[info["preinstalled"]])
        self.assertIn("missing", lines["definitely-not-real-xyz"])

    def test_dry_run_installs_nothing(self):
        info = DISTROS[self.distro]
        proc = self.run_in(self.fixture("""
            python3 sync.py packages install --dry-run
            command -v {inst} && {{ echo WAS_INSTALLED; exit 1; }}
            echo DRYRUN_OK
        """.format(inst=info["installable"])))
        self.assertIn("DRYRUN_OK", proc.stdout)
        self.assertNotIn("WAS_INSTALLED", proc.stdout)

    def test_real_install_flips_status(self):
        """Actually installs a package with the distro's manager."""
        info = DISTROS[self.distro]
        proc = self.run_in("""
            cat > /repo/packages.json <<'EOF'
{{ "{inst}": {{}} }}
EOF
            python3 sync.py packages status | grep -q missing || {{ echo NOT_MISSING; exit 1; }}
            python3 sync.py packages install
            command -v {inst} >/dev/null || {{ echo BINARY_ABSENT; exit 1; }}
            python3 sync.py packages status | grep -q installed || {{ echo NOT_INSTALLED; exit 1; }}
            echo INSTALL_OK
        """.format(inst=info["installable"]))
        self.assertIn("INSTALL_OK", proc.stdout)

    def test_release_install_over_http(self):
        """Full release path on this distro, served from inside the container."""
        proc = self.run_in(r"""
            mkdir -p /fixtures/toy-1.0.0
            printf '#!/bin/sh\necho toy 1.0.0\n' > /fixtures/toy-1.0.0/toy
            chmod +x /fixtures/toy-1.0.0/toy
            (cd /fixtures && tar czf toy-1.0.0.tar.gz toy-1.0.0)
            (cd /fixtures && python3 -m http.server 8899 >/dev/null 2>&1 &)
            for i in 1 2 3 4 5 6 7 8 9 10; do
                python3 -c "import socket,sys; s=socket.socket(); sys.exit(s.connect_ex(('127.0.0.1',8899)))" && break
                sleep 0.3
            done
            cat > /repo/packages.json <<'EOF'
{
	"toy": {
		"apt": null, "pacman": null, "dnf": null, "brew": null,
		"release": {
			"url": "http://127.0.0.1:8899/toy-{version}.tar.gz",
			"version": "1.0.0",
			"bin": "toy-{version}/toy"
		}
	}
}
EOF
            python3 sync.py packages install
            test -x $HOME/.local/bin/toy || { echo NOT_EXECUTABLE; exit 1; }
            [ "$($HOME/.local/bin/toy)" = "toy 1.0.0" ] || { echo BAD_OUTPUT; exit 1; }
            test -f $HOME/.local/state/personal-config/installed.json || { echo NO_RECEIPT; exit 1; }
            python3 sync.py packages status | grep -q installed || { echo NOT_INSTALLED; exit 1; }
            echo RELEASE_OK
        """)
        self.assertIn("RELEASE_OK", proc.stdout)

    def test_runs_as_root_without_sudo(self):
        """As root, commands must not be prefixed with sudo -- even on images
        that ship sudo, since the decision keys off euid, not availability."""
        proc = self.run_in("""
            cat > /repo/packages.json <<'EOF'
{ "fake-pkg-xyz": {} }
EOF
            python3 sync.py packages install --dry-run | grep -q 'sudo' && { echo USED_SUDO; exit 1; }
            echo NOSUDO_OK
        """)
        self.assertIn("NOSUDO_OK", proc.stdout)
        self.assertNotIn("USED_SUDO", proc.stdout)


class TestDebian(ManagedDistroCase):
    distro = "debian"


class TestArch(ManagedDistroCase):
    distro = "arch"


class TestFedora(ManagedDistroCase):
    distro = "fedora"


class TestAlpine(ContainerCase):
    """No apt/pacman/dnf: the tool must degrade, not crash."""

    distro = "alpine"

    def test_reports_no_manager_and_skips(self):
        proc = self.run_in("""
            cat > /repo/packages.json <<'EOF'
{ "ripgrep": {} }
EOF
            python3 sync.py packages status
            echo NOMGR_OK
        """)
        self.assertIn("none detected", proc.stdout)
        self.assertIn("skipped", proc.stdout)
        self.assertIn("NOMGR_OK", proc.stdout)

    def test_install_with_no_manager_does_nothing(self):
        proc = self.run_in("""
            cat > /repo/packages.json <<'EOF'
{ "ripgrep": {} }
EOF
            python3 sync.py packages install
            echo NOINSTALL_OK
        """)
        self.assertIn("nothing to install", proc.stdout)

    def test_release_still_works_without_a_manager(self):
        """Release installs shouldn't depend on a system package manager at all."""
        proc = self.run_in(r"""
            mkdir -p /fixtures/toy-1.0.0
            printf '#!/bin/sh\necho toy 1.0.0\n' > /fixtures/toy-1.0.0/toy
            chmod +x /fixtures/toy-1.0.0/toy
            (cd /fixtures && tar czf toy-1.0.0.tar.gz toy-1.0.0)
            (cd /fixtures && python3 -m http.server 8899 >/dev/null 2>&1 &)
            for i in 1 2 3 4 5 6 7 8 9 10; do
                python3 -c "import socket,sys; s=socket.socket(); sys.exit(s.connect_ex(('127.0.0.1',8899)))" && break
                sleep 0.3
            done
            cat > /repo/packages.json <<'EOF'
{
	"toy": {
		"release": {
			"url": "http://127.0.0.1:8899/toy-{version}.tar.gz",
			"version": "1.0.0",
			"bin": "toy-{version}/toy"
		}
	}
}
EOF
            python3 sync.py packages install
            [ "$($HOME/.local/bin/toy)" = "toy 1.0.0" ] || { echo BAD_OUTPUT; exit 1; }
            echo RELEASE_OK
        """)
        self.assertIn("RELEASE_OK", proc.stdout)


# Only the concrete per-distro classes should run.
del ContainerCase, ManagedDistroCase


if __name__ == "__main__":
    unittest.main()
