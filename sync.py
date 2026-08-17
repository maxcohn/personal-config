#!/usr/bin/env python3
"""Manage this repo's machine configs and software.

    ./sync.py status   [module...]             repo vs system, per-file state
    ./sync.py diff     [module...]             unified diffs, repo vs system
    ./sync.py deploy   [module...] [--dry-run] copy repo -> system
    ./sync.py capture  [module...] [--dry-run] copy system -> repo (tracked files only)
    ./sync.py packages status|install [--dry-run] [--manager NAME]

Stdlib only; targets Python 3.8+. See README.md for the manifest and
packages.json schemas.
"""

import argparse
import difflib
import fnmatch
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent
HOME = Path.home()
MODULES_DIR = REPO / "modules"
PACKAGES_FILE = REPO / "packages.json"
RECEIPTS_FILE = HOME / ".local" / "state" / "personal-config" / "installed.json"
LOCAL_BIN = HOME / ".local" / "bin"

SYSTEM_MANAGERS = ["apt", "pacman", "dnf", "brew"]
LANG_INSTALLERS = ["cargo", "go", "uv"]
NON_METHOD_KEYS = {"prefer", "bin", "_"}  # "_" is a per-package comment

DEFAULT_OS_MAP = {"linux": "linux", "darwin": "darwin"}
DEFAULT_ARCH_MAP = {"x86_64": "x86_64", "amd64": "x86_64", "arm64": "aarch64", "aarch64": "aarch64"}


# ---------------------------------------------------------------------------
# Config modules
# ---------------------------------------------------------------------------

class Module:
    def __init__(self, name, path, manifest):
        self.name = name
        self.path = path
        self.destination = Path(os.path.expanduser(manifest["destination"]))
        self.ignore = manifest.get("ignore", [])

    def files(self):
        """Managed files as paths relative to the module directory."""
        out = []
        for p in sorted(self.path.rglob("*")):
            if not p.is_file():
                continue
            rel = p.relative_to(self.path)
            if rel.as_posix() == "manifest.json":
                continue
            if self.is_ignored(rel):
                continue
            out.append(rel)
        return out

    def is_ignored(self, rel):
        rel_posix = rel.as_posix()
        return any(fnmatch.fnmatch(rel_posix, pat) or fnmatch.fnmatch(rel.name, pat)
                   for pat in self.ignore)


def load_modules(names=None):
    modules = {}
    if MODULES_DIR.is_dir():
        for entry in sorted(MODULES_DIR.iterdir()):
            manifest_path = entry / "manifest.json"
            if entry.is_dir() and manifest_path.is_file():
                with open(manifest_path) as f:
                    modules[entry.name] = Module(entry.name, entry, json.load(f))
    if names:
        unknown = [n for n in names if n not in modules]
        if unknown:
            sys.exit("unknown module(s): {} (available: {})".format(
                ", ".join(unknown), ", ".join(modules)))
        return [modules[n] for n in names]
    return list(modules.values())


def files_differ(a, b):
    if not a.exists() or not b.exists():
        return True
    return a.read_bytes() != b.read_bytes()


def cmd_status(args):
    drift = False
    for mod in load_modules(args.modules):
        lines = []
        for rel in mod.files():
            dest = mod.destination / rel
            if not dest.exists():
                lines.append(("missing", rel, dest))
            elif files_differ(mod.path / rel, dest):
                lines.append(("modified", rel, dest))
        print("{}  ->  {}".format(mod.name, mod.destination))
        if lines:
            drift = True
            for state, rel, dest in lines:
                print("  {:<9} {}  ({})".format(state, rel, dest))
        else:
            print("  ok ({} files)".format(len(mod.files())))
    return 1 if drift else 0


def cmd_diff(args):
    for mod in load_modules(args.modules):
        for rel in mod.files():
            repo_file = mod.path / rel
            dest = mod.destination / rel
            if not dest.exists():
                print("--- {} (repo)\n+++ {} (missing on system)".format(repo_file, dest))
                continue
            if not files_differ(repo_file, dest):
                continue
            try:
                a = repo_file.read_text().splitlines(keepends=True)
                b = dest.read_text().splitlines(keepends=True)
            except UnicodeDecodeError:
                print("Binary files {} and {} differ".format(repo_file, dest))
                continue
            sys.stdout.writelines(difflib.unified_diff(
                a, b, fromfile="{} (repo)".format(repo_file), tofile="{} (system)".format(dest)))
    return 0


def cmd_deploy(args):
    for mod in load_modules(args.modules):
        for rel in mod.files():
            repo_file = mod.path / rel
            dest = mod.destination / rel
            if not files_differ(repo_file, dest):
                continue
            print("deploy: {} -> {}".format(repo_file.relative_to(REPO), dest))
            if not args.dry_run:
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(repo_file, dest)
    return 0


def cmd_capture(args):
    for mod in load_modules(args.modules):
        for rel in mod.files():
            repo_file = mod.path / rel
            dest = mod.destination / rel
            if not dest.exists():
                print("capture: {} missing on system, skipped".format(dest))
                continue
            if not files_differ(repo_file, dest):
                continue
            print("capture: {} -> {}".format(dest, repo_file.relative_to(REPO)))
            if not args.dry_run:
                shutil.copy2(dest, repo_file)
    return 0


# ---------------------------------------------------------------------------
# Packages
# ---------------------------------------------------------------------------

def detect_system_manager(override=None):
    """Identify what system manager should be used on the current system."""
    if override:
        return override
    if platform.system() == "Darwin":
        return "brew" if shutil.which("brew") else None
    for mgr in ("apt", "pacman", "dnf"):
        if shutil.which(mgr):
            return mgr
    return None


def load_packages():
    with open(PACKAGES_FILE) as f:
        return json.load(f)


def load_receipts():
    if RECEIPTS_FILE.is_file():
        with open(RECEIPTS_FILE) as f:
            return json.load(f)
    return {}


def save_receipt(name, method, version):
    receipts = load_receipts()
    receipts[name] = {"method": method, "version": version}
    RECEIPTS_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(RECEIPTS_FILE, "w") as f:
        json.dump(receipts, f, indent=2)


def resolve(entry, sysmgr):
    """Ordered (method, spec) candidates for a package entry."""
    candidates = []
    if sysmgr and entry.get(sysmgr) is not None:
        candidates.append((sysmgr, entry[sysmgr]))
    for lang in LANG_INSTALLERS:
        if entry.get(lang) is not None and shutil.which(lang):
            candidates.append((lang, entry[lang]))
    if entry.get("release") is not None:
        candidates.append(("release", entry["release"]))
    if entry.get("build") is not None:
        candidates.append(("build", entry["build"]))
    prefer = entry.get("prefer")
    if prefer:
        candidates.sort(key=lambda c: c[0] != prefer)
    return candidates


def binary_name(name, entry):
    return entry.get("bin", name)


def system_pkg_installed(mgr, spec):
    pkg = spec.split(":", 1)[1] if ":" in spec else spec
    queries = {
        "apt": ["dpkg", "-s", pkg],
        "pacman": ["pacman", "-Qi", pkg],
        "dnf": ["rpm", "-q", pkg],
        "brew": ["brew", "list", "--cask" if spec.startswith("cask:") else "--formula", pkg],
    }
    return subprocess.run(queries[mgr], stdout=subprocess.DEVNULL,
                          stderr=subprocess.DEVNULL).returncode == 0


def package_state(name, entry, method, spec):
    """Checks the state of the package on the system.

    One of: installed, outdated (release only), missing.
    """
    if method in SYSTEM_MANAGERS:
        # For system managers, we use the package managers themselves to know if it's installed
        return "installed" if system_pkg_installed(method, spec) else "missing"

    # For other install methods, we check if the binary of that name exists on the system
    binary = binary_name(name, entry)

    if method == "release":
        # Don't rely on PATH. We know exactly where release binaries land.
        if not (LOCAL_BIN / binary).is_file():
            return "missing"
        receipt = load_receipts().get(name)
        if receipt and receipt.get("version") != spec["version"]:
            return "outdated ({} -> {})".format(receipt["version"], spec["version"])
        return "installed"
    return "installed" if shutil.which(binary) else "missing"


def release_url_and_bin(name, spec):
    sysname = platform.system().lower()
    machine = platform.machine().lower()
    os_val = spec.get("os_map", DEFAULT_OS_MAP).get(sysname, sysname)
    arch_val = spec.get("arch_map", DEFAULT_ARCH_MAP).get(machine, machine)
    url = spec["url"].format(version=spec["version"], os=os_val, arch=arch_val)
    bin_in_archive = spec.get("bin")
    if bin_in_archive:
        bin_in_archive = bin_in_archive.format(version=spec["version"], os=os_val, arch=arch_val)
    return url, bin_in_archive


def safe_extract(archive, dest_dir):
    if isinstance(archive, tarfile.TarFile):
        members = archive.getmembers()
        names = [m.name for m in members]
    else:
        members = None
        names = archive.namelist()
    for n in names:
        if n.startswith("/") or ".." in Path(n).parts:
            sys.exit("refusing to extract unsafe path in archive: {}".format(n))
    if members is not None:
        # Python 3.12+ warns without an explicit filter and defaults to 'data' in
        # 3.14; ask for it when supported, but keep working on older pythons.
        try:
            archive.extractall(dest_dir, members=members, filter="data")
        except TypeError:
            archive.extractall(dest_dir, members=members)
    else:
        archive.extractall(dest_dir)


def install_release(name, entry, spec, dry_run):
    url, bin_in_archive = release_url_and_bin(name, spec)
    target = LOCAL_BIN / binary_name(name, entry)
    print("  fetch {}".format(url))
    print("  install -> {}".format(target))
    if dry_run:
        return
    LOCAL_BIN.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        download = tmp / Path(url).name
        urllib.request.urlretrieve(url, download)
        lower = download.name.lower()
        if lower.endswith((".tar.gz", ".tgz", ".tar.xz", ".tar.bz2", ".tar")):
            with tarfile.open(download) as tf:
                safe_extract(tf, tmp / "x")
            src = tmp / "x" / bin_in_archive
        elif lower.endswith(".zip"):
            with zipfile.ZipFile(download) as zf:
                safe_extract(zf, tmp / "x")
            src = tmp / "x" / bin_in_archive
        else:
            src = download  # raw binary
        if not src.is_file():
            sys.exit("release: expected binary not found in archive: {}".format(src))
        shutil.copy2(src, target)
        target.chmod(target.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    save_receipt(name, "release", spec["version"])


def run_or_print(cmd, dry_run):
    print("  $ {}".format(" ".join(cmd)))
    if not dry_run:
        subprocess.run(cmd, check=True)


def sudo_prefix():
    return [] if os.geteuid() == 0 else ["sudo"]


def install_via_system(mgr, specs, dry_run):
    """specs: list of package specs for this manager (may carry cask:/aur: prefixes)."""
    plain = [s for s in specs if ":" not in s]
    casks = [s.split(":", 1)[1] for s in specs if s.startswith("cask:")]
    aur = [s.split(":", 1)[1] for s in specs if s.startswith("aur:")]
    cmds = {
        "apt": sudo_prefix() + ["apt", "install", "-y"],
        "pacman": sudo_prefix() + ["pacman", "-S", "--needed", "--noconfirm"],
        "dnf": sudo_prefix() + ["dnf", "install", "-y"],
        "brew": ["brew", "install"],
    }
    if plain:
        run_or_print(cmds[mgr] + plain, dry_run)
    if casks:
        run_or_print(["brew", "install", "--cask"] + casks, dry_run)
    if aur:
        helper = next((h for h in ("paru", "yay") if shutil.which(h)), None)
        if helper:
            run_or_print([helper, "-S", "--needed", "--noconfirm"] + aur, dry_run)
        else:
            print("  no AUR helper (paru/yay) found; install manually: {}".format(", ".join(aur)))


def install_via_lang(lang, spec, dry_run):
    if lang == "cargo":
        if "@" in spec:
            crate, version = spec.rsplit("@", 1)
            run_or_print(["cargo", "install", crate, "--version", version], dry_run)
        else:
            run_or_print(["cargo", "install", spec], dry_run)
    elif lang == "go":
        run_or_print(["go", "install", spec if "@" in spec else spec + "@latest"], dry_run)
    elif lang == "uv":
        if "@" in spec:
            pkg, version = spec.rsplit("@", 1)
            run_or_print(["uv", "tool", "install", "{}=={}".format(pkg, version)], dry_run)
        else:
            run_or_print(["uv", "tool", "install", spec], dry_run)


def cmd_packages(args):
    packages = load_packages()
    sysmgr = detect_system_manager(args.manager)
    if sysmgr and not shutil.which(sysmgr):
        sys.exit("package manager not found on PATH: {}".format(sysmgr))
    print("system package manager: {}".format(sysmgr or "none detected"))
    if str(LOCAL_BIN) not in os.environ.get("PATH", "").split(os.pathsep):
        print("warning: {} is not on PATH; release-installed tools won't be runnable"
              .format(LOCAL_BIN))

    resolved = {}  # name -> (method, spec, state)
    for name, entry in packages.items():
        candidates = resolve(entry, sysmgr)
        if not candidates:
            resolved[name] = (None, None, "skipped (no available method)")
            continue
        method, spec = candidates[0]
        if method == "build":
            resolved[name] = (method, spec, "build: not yet implemented -- install manually")
            continue
        resolved[name] = (method, spec, package_state(name, entry, method, spec))

    if args.action == "status":
        for name, (method, spec, state) in resolved.items():
            via = "" if method is None else "  (via {}: {})".format(
                method, spec if isinstance(spec, str) else spec.get("version", "?"))
            print("  {:<16} {}{}".format(name, state, via))
        return 0

    # install
    to_install = {n: v for n, v in resolved.items()
                  if v[2] == "missing" or v[2].startswith("outdated")}
    if not to_install:
        print("nothing to install")
        return 0
    system_batches = {}  # mgr -> [specs]
    for name, (method, spec, state) in to_install.items():
        if method in SYSTEM_MANAGERS:
            system_batches.setdefault(method, []).append(spec)
    for mgr, specs in system_batches.items():
        print("{}: {}".format(mgr, ", ".join(specs)))
        install_via_system(mgr, specs, args.dry_run)
    for name, (method, spec, state) in to_install.items():
        if method in LANG_INSTALLERS:
            print("{} ({})".format(name, method))
            install_via_lang(method, spec, args.dry_run)
        elif method == "release":
            print("{} (release {})".format(name, spec["version"]))
            install_release(name, packages[name], spec, args.dry_run)
    return 0


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    for name, fn in (("status", cmd_status), ("diff", cmd_diff),
                     ("deploy", cmd_deploy), ("capture", cmd_capture)):
        p = sub.add_parser(name)
        p.add_argument("modules", nargs="*", help="module names (default: all)")
        if name in ("deploy", "capture"):
            p.add_argument("--dry-run", action="store_true")
        p.set_defaults(fn=fn)

    p = sub.add_parser("packages")
    p.add_argument("action", choices=["status", "install"])
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--manager", help="override detected system package manager")
    p.set_defaults(fn=cmd_packages)

    args = parser.parse_args()
    sys.exit(args.fn(args))


if __name__ == "__main__":
    main()
