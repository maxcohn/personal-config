"""Shared fixtures for the sync.py test suite.

Everything here builds throwaway repos and HOMEs under tempfile, so no test ever
touches the real machine.
"""

import http.server
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SYNC_PY = REPO_ROOT / "sync.py"


class Sandbox(unittest.TestCase):
    """Base class giving each test a fresh temp repo + temp HOME."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pc-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.repo = self.tmp / "repo"
        self.modules = self.repo / "modules"
        self.home = self.tmp / "home"
        self.modules.mkdir(parents=True)
        self.home.mkdir()
        shutil.copy2(SYNC_PY, self.repo / "sync.py")

    # -- building fixtures --------------------------------------------------

    def write(self, path, content=""):
        """Write a file under the sandbox, creating parents. Returns the path."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            path.write_bytes(content)
        else:
            path.write_text(content)
        return path

    def module(self, name, destination, files, ignore=None):
        """Create a repo module: files is {relpath: content}."""
        manifest = {"destination": destination}
        if ignore is not None:
            manifest["ignore"] = ignore
        mod = self.modules / name
        mod.mkdir(parents=True, exist_ok=True)
        self.write(mod / "manifest.json", json.dumps(manifest))
        for rel, content in files.items():
            self.write(mod / rel, content)
        return mod

    def packages(self, data):
        self.write(self.repo / "packages.json", json.dumps(data))

    # -- running sync.py ----------------------------------------------------

    def sync(self, *args, home=None, path=None, expect=None):
        """Run sync.py in the sandbox. Returns CompletedProcess."""
        env = dict(os.environ)
        env["HOME"] = str(home or self.home)
        env["PATH"] = path if path is not None else env.get("PATH", "")
        proc = subprocess.run(
            [sys.executable, str(self.repo / "sync.py"), *args],
            capture_output=True, text=True, env=env, cwd=str(self.repo))
        if expect is not None:
            self.assertEqual(
                proc.returncode, expect,
                "expected exit {}, got {}\nstdout:\n{}\nstderr:\n{}".format(
                    expect, proc.returncode, proc.stdout, proc.stderr))
        return proc

    # -- assertions ---------------------------------------------------------

    def snapshot(self, root):
        """Map of relpath -> bytes for every file under root."""
        root = Path(root)
        return {str(p.relative_to(root)): p.read_bytes()
                for p in sorted(root.rglob("*")) if p.is_file()}

    def assertUnchanged(self, before, root, msg="filesystem was modified"):
        self.assertEqual(before, self.snapshot(root), msg)


# ---------------------------------------------------------------------------
# Release-download fixtures: a local HTTP server so tests never hit the network
# ---------------------------------------------------------------------------

TOY_SCRIPT = "#!/bin/sh\necho toy {version}\n"


def make_toy_tarball(path, version, compression="gz"):
    """Tarball containing toy-<version>/toy, an executable script."""
    path = Path(path)
    staging = path.parent / "staging-{}".format(version)
    inner = staging / "toy-{}".format(version)
    inner.mkdir(parents=True, exist_ok=True)
    script = inner / "toy"
    script.write_text(TOY_SCRIPT.format(version=version))
    script.chmod(0o755)
    with tarfile.open(path, "w:{}".format(compression) if compression else "w") as tf:
        tf.add(inner, arcname=inner.name)
    shutil.rmtree(staging)
    return path


def make_toy_zip(path, version):
    path = Path(path)
    with zipfile.ZipFile(path, "w") as zf:
        info = zipfile.ZipInfo("toy-{}/toy".format(version))
        info.external_attr = 0o755 << 16
        zf.writestr(info, TOY_SCRIPT.format(version=version))
    return path


def make_raw_binary(path, version):
    path = Path(path)
    path.write_text(TOY_SCRIPT.format(version=version))
    path.chmod(0o755)
    return path


def make_malicious_tar(path, name):
    """Tarball with an unsafe member name, for safe_extract tests."""
    path = Path(path)
    with tarfile.open(path, "w") as tf:
        data = b"pwned\n"
        info = tarfile.TarInfo(name)
        info.size = len(data)
        import io
        tf.addfile(info, io.BytesIO(data))
    return path


def make_malicious_zip(path, name):
    path = Path(path)
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(name, "pwned\n")
    return path


class FileServer:
    """Serves a directory over HTTP on an ephemeral localhost port."""

    def __init__(self, directory):
        self.directory = str(directory)
        handler = self._make_handler(self.directory)
        self.httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    @staticmethod
    def _make_handler(directory):
        class Handler(http.server.SimpleHTTPRequestHandler):
            def __init__(self, *a, **kw):
                super().__init__(*a, directory=directory, **kw)

            def log_message(self, *a):
                pass  # keep test output clean
        return Handler

    @property
    def url(self):
        return "http://127.0.0.1:{}".format(self.port)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


# ---------------------------------------------------------------------------
# Container runtime detection (tier 2)
# ---------------------------------------------------------------------------

def container_runtime():
    """podman preferred (works rootless); docker fallback. None if unusable."""
    for runtime in ("podman", "docker"):
        if not shutil.which(runtime):
            continue
        probe = subprocess.run([runtime, "info"],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        if probe.returncode == 0:
            return runtime
    return None
