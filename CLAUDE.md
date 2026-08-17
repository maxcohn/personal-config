# Working in this repo

`README.md` is the spec. It documents every schema and convention here, and it is
kept accurate — when behaviour changes, the README changes in the same commit.

## Comments

Keep comments small and meaningful. A comment earns its place by explaining
something the code cannot: why a decision was made, a non-obvious constraint, a
trap someone would otherwise fall into. Never restate what the line already says.

```python
# Good -- the reason isn't inferable from the code:
# apt(8) warns it has no stable CLI when its output isn't a terminal; apt-get does not.
REFRESH_CMDS = {"apt": ["apt-get", "update"]}

# Bad -- says nothing the code doesn't:
# Set the refresh commands dict
REFRESH_CMDS = {"apt": ["apt-get", "update"]}
```

The same applies to docstrings: one line on what a function is for, and only then
a second paragraph if there's a real subtlety to flag. No parameter lists that
just re-spell the signature.

Test names carry the intent, so most tests need no comment. Where a test exists to
guard a specific past mistake or a security property, say so in one line — that's
the sentence that stops someone deleting it later.

## Code

- Python 3.8+, **standard library only**. Nothing to install before bootstrapping
  a new machine, and no dependency can break a fresh setup.
- `sync.py` is deliberately one file.
- Every privileged or mutating action goes through `run_or_print`, so `--dry-run`
  and command echoing come for free. Never call `subprocess` directly for those.
- Nothing is ever inferred. A package name, a version, a manager — if it isn't
  stated in `packages.json`, it doesn't happen. `null` means "checked, and no".
- Anything installed outside a system package manager is pinned in this repo, so
  upgrading is an explicit commit and machines don't drift on their own.
- The tool only ever adds and repairs. It never removes or downgrades.

## Tests

`./test.py` before calling anything done; `./test.py --containers` when the change
touches a package manager or a privileged path.

Nothing may touch the real machine. Tier 1 runs against a temp `HOME`, a temp copy
of the repo, and a temp `PERSONAL_CONFIG_SYSROOT`; tier 2 mounts the repo
read-only into throwaway containers. If a new code path can write outside those,
it needs a seam before it needs a test.

Validation of `packages.json` and the module manifests lives in
`tests/test_manifest.py` and runs against the **real** files, so a typo fails the
suite rather than surfacing on a machine. A new schema key belongs there too.

## Commits

Data-only additions are short and specific: `package: neofetch (apt)`. Code
changes bring their tests and their README update along in the same commit.
