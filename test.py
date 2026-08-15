#!/usr/bin/env python3
"""Test suite for sync.py.

    ./test.py                  tier 1 only: fast, no container runtime needed
    ./test.py --containers     also run tier 2 (real package managers, per distro)
    ./test.py --distro arch    restrict tier 2 to one image
    ./test.py --rebuild        force container images to rebuild
    ./test.py -v               verbose

Stdlib unittest, no dependencies. Nothing here touches the real HOME or repo:
tier 1 runs against temp directories, tier 2 inside throwaway containers with the
repo mounted read-only.
"""

import argparse
import os
import sys
import unittest
from pathlib import Path

# Keep the repo tidy: importing sync.py would otherwise drop __pycache__ dirs.
sys.dont_write_bytecode = True

TESTS = Path(__file__).resolve().parent / "tests"

TIER1 = ["test_units", "test_manifest", "test_cli"]
TIER2 = ["test_containers"]


def main():
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--containers", action="store_true",
                        help="also run container-based tests (slower)")
    parser.add_argument("--distro", choices=["debian", "arch", "fedora", "alpine"],
                        help="restrict container tests to one distro (implies --containers)")
    parser.add_argument("--rebuild", action="store_true",
                        help="force rebuild of container images")
    parser.add_argument("-v", "--verbose", action="store_true")
    parser.add_argument("-k", "--filter", help="only run tests matching this substring")
    args = parser.parse_args()

    if args.distro:
        args.containers = True
        os.environ["PC_TEST_DISTRO"] = args.distro
    if args.rebuild:
        os.environ["PC_TEST_REBUILD"] = "1"

    sys.path.insert(0, str(TESTS))
    modules = TIER1 + (TIER2 if args.containers else [])

    loader = unittest.TestLoader()
    if args.filter:
        loader.testNamePatterns = ["*{}*".format(args.filter)]
    suite = unittest.TestSuite(loader.loadTestsFromName(m) for m in modules)

    if not args.containers:
        print("tier 1 only -- pass --containers to exercise real package managers\n")

    result = unittest.TextTestRunner(verbosity=2 if args.verbose else 1).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(main())
