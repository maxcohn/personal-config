# personal-config

My machine configs, plus the tooling to move them onto a machine and keep track
of the software I expect to be installed. Works across Linux distros and macOS.

Configs are copied onto the machine and get synced on demand.

## Usage

```sh
./sync.py status   [module...]              # per-file: ok / modified / missing
./sync.py diff     [module...]              # unified diff, repo vs machine
./sync.py deploy   [module...] [--dry-run]  # repo -> machine
./sync.py capture  [module...] [--dry-run]  # machine -> repo (tracked files only)

./sync.py packages status  [--manager NAME]
./sync.py packages install [--dry-run] [--manager NAME]

./sync.py repos status     [--manager NAME]
./sync.py repos install    [--dry-run] [--manager NAME]
./sync.py repos fetch-key  <package>...
```

With no module arguments, every module is used. `status` exits non-zero when
anything has drifted, so it works in a prompt or a cron check. `repos status`
follows the same rule.

Python 3.8+, standard library only - nothing to install before bootstrapping a
new machine.

## Modules

Config lives under `modules/`, one directory per module.

```
modules/
	vim/     manifest.json, vimrc, colors/, autoload/, spell/
	zsh/     manifest.json, .zshrc, .zaliases
```

A module is any directory inside `modules/` containing a `manifest.json`. Adding
one is just creating the directory; there is no central registry.

```json
{
	"destination": "~/.vim/",
	"ignore": [".netrwhist"]
}
```

- **`destination`** - the directory the module's files map into, preserving
  relative paths. `modules/zsh/.zshrc` -> `~/.zshrc`;
  `modules/vim/colors/onedark.vim` -> `~/.vim/colors/onedark.vim`.
- **`ignore`** - glob patterns skipped in both directions. Matched against both
  the path relative to the module and the bare filename. `manifest.json` is
  always skipped.

**The files in the repo define what's managed.** `capture` only pulls back files
the repo already has, so plugin directories, history files, and other runtime
junk in the destination are ignored. To start tracking a new file, add it to the
module directory yourself.

## Machine-local config

Anything machine-specific or private (secrets, work aliases, host-specific
paths) goes in an untracked `.local` file that the tracked config sources if it
exists:

| Tracked, deployed | Untracked, per-machine |
| --- | --- |
| `~/.zshrc` | `~/.zshrc.local` |
| `~/.vim/vimrc` | `~/.vimrc.local` |

This repo is public - nothing private should ever land in a tracked file.

## Software (`packages.json`)

One entry per logical tool. The key is identification only - how you refer to
the tool here, and the default name of the binary it puts on the machine.

Each package manager can be specified if it can be installed with it.

```json
{
	"ripgrep": { "apt": "ripgrep", "brew": "ripgrep" },
	"fd": { "apt": "fd-find", "bin": "fdfind" },
	"xclip": { "apt": "xclip", "brew": null },
	"eza": { "_": "This is a comment", "apt": null, "cargo": "eza@0.18.0" },
	"shellcheck": {
		"release": {
			"url": "https://github.com/koalaman/shellcheck/releases/download/v{version}/shellcheck-v{version}.{os}.{arch}.tar.xz",
			"version": "0.10.0",
			"bin": "shellcheck-v{version}/shellcheck"
		},
		"prefer": "release"
	}
}
```

Versions of anything installed outside a system package manager are **pinned in
this repo**. Upgrading a tool is an explicit commit, so machines don't drift
apart on their own.

### Install methods

**System managers** - `apt`, `pacman`, `dnf`, `brew`. Give a string naming the
package on that manager; give `null` to record that it's deliberately
unavailable there. Omitting the key has the same effect as `null` - the manager
won't install the tool - so `null` is worth writing when the answer is *checked
and no* rather than *not looked into yet*. Prefix a brew value with `cask:` for
casks, or a pacman value with `aur:` to route it through `paru`/`yay` (reported
as manual if neither is installed).

**Language installers** - `cargo`, `go`, `uv`. Value is `name` or
`name@version`; for `go`, the full module path. Only considered when the
toolchain is on `PATH`.

**`release`** - a prebuilt binary from a release page.

| Field | Meaning |
| --- | --- |
| `url` | Template; `{version}`, `{os}`, `{arch}` are substituted |
| `version` | Pinned version |
| `bin` | Path to the executable inside the archive (same substitutions as `url`) |
| `os_map`, `arch_map` | Optional overrides when a project spells platforms its own way |

Defaults are `linux`/`darwin` and `x86_64`/`aarch64`. `.tar.gz`, `.tar.xz`,
`.zip`, and raw binaries are handled; the binary lands in `~/.local/bin/`.
Installed versions are recorded in
`~/.local/state/personal-config/installed.json` (machine-local, untracked) so
`status` can report *outdated* against the pinned version without shelling out
to `--version`. Detection looks in `~/.local/bin` directly rather than at `PATH`,
so a release install is still reported correctly on a machine that hasn't been
set up yet - but `packages` warns when `~/.local/bin` isn't on `PATH`, since the
tools won't actually be runnable there.

**`prefer`** - name a method to use even when an earlier one is available.
Without it, the order is: system manager -> language installer (if the toolchain
exists) -> `release` -> `build`. The first available method wins.

`install` only ever adds things: it installs what's missing and re-installs
`release` entries whose pinned version moved. It never removes or downgrades.

**Comments** - Comments can be specified per package with a key of an underscore (`_`).

### Third-party repositories

Some tools only exist in a vendor's own repository, or ship there far newer than
the distro's copy. A `repo` block, keyed by manager, declares that repository
alongside the package that needs it:

```json
"github-cli": {
	"apt": "gh",
	"brew": "gh",
	"repo": {
		"apt": {
			"uris": "https://cli.github.com/packages",
			"suites": "stable",
			"components": "main",
			"key": "keys/github-cli.asc",
			"key_url": "https://cli.github.com/packages/githubcli-archive-keyring.gpg"
		}
	}
}
```

Only `apt` is implemented. The block renders to a deb822
`/etc/apt/sources.list.d/<file>.sources`.

The inner object is that manager's own vocabulary, not a shared schema - these
field names are deb822's, lowercased, so `man 5 sources.list` is the reference and
there's nothing to translate. dnf would spell almost all of it differently
(`baseurl`, `metalink`, `gpgkey`), which is why the manager key exists.

| Field | Meaning |
| --- | --- |
| `uris`, `suites`, `components` | Required. The deb822 fields of the same name |
| `key` | Repo-relative path to the vendored signing key. Installed to `/etc/apt/keyrings/` and named in `Signed-By` |
| `key_url` | Where the key came from. Read by `fetch-key`, **never** at install time |
| `types` | Defaults to `deb` |
| `architectures` | Omitted by default, which lets apt use its own architecture list. Set it for a repo that publishes one arch - or on any machine with a foreign architecture enabled, where apt would otherwise probe every vendor repo for `i386` |
| `file` | Source-file stem, defaulting to the package key. Two packages sharing a `file` collapse onto one source file |

Values are written through **verbatim** - there is no templating. The one
exception is the keyword `"auto"`, accepted by `suites` (resolved from
`VERSION_CODENAME` in `/etc/os-release`) and `architectures` (from `dpkg
--print-architecture`), for the many repositories keyed to the release codename.

`suites: "auto"` only makes sense when `uris` is distro-agnostic. A vendor that
splits by distro forces a literal URI, and pairing that with `"auto"` asks an
Ubuntu-only repository for a Debian codename - so docker and tailscale pin their
suite the same way they pin their URI, and a release upgrade is an explicit
commit.

A repository is only added when the package would actually be installed from
that manager here, so a tool that comes from `cargo` on this machine doesn't drag
its apt repository along, and the whole block is inert on macOS.

`repos install` writes only what is missing or has drifted, and runs `apt-get
update` **once, only when something actually changed**. It rewrites a file that
was edited by hand, but never removes a repository you stopped declaring.

On a fresh machine, `repos install` comes **before** `packages install` - a tool
that lives in a vendor's repository can't be found until that repository exists.

### Adding a repository

1. Add the entry to `packages.json` with its `repo.apt` block. `suites: "auto"`
   for a repo keyed to the release codename *and* served from one distro-agnostic
   URI; `architectures: "auto"` unless you know you want apt's full list.
2. `./sync.py repos fetch-key <name>`, then read the key before `git add` - see
   below for why that review is the point.
3. `./test.py -k repo` validates the block and the key's armor against the real
   file, so a typo fails here rather than on a machine.
4. `./sync.py repos install --dry-run`, then for real.
5. If the repository already existed as a hand-written `.list`, delete it. sync.py
   only writes deb822 `.sources`, so the old file would sit alongside the managed
   one and apt would index the repository twice.

### Signing keys (`keys/`)

Keys are **committed to this repo, ASCII-armored**, and never fetched at install
time. Installing a vendor's signing key hands them root on the machine, so the
exact bytes being trusted should be reviewable in `git log` and the decision made
once, deliberately, rather than re-taken from a URL on every machine. It also
means `repos status` can report a rotated key as drift, and that a fresh machine
needs no network to lay the key down.

To add one, put `key_url` in the entry and let sync.py fetch it:

```sh
./sync.py repos fetch-key github-cli   # writes keys/github-cli.asc
git add keys/github-cli.asc            # review it, then commit
```

Most vendors serve a binary `.gpg`; `fetch-key` armors it on the way in so
everything in `keys/` stays diffable text. A vendor that already serves armor is
decoded and re-armored rather than passed through, because they disagree about
armor headers - Microsoft ships a `Version:` line, most ship none - and those
would otherwise land in the diff looking like key changes. The vendor's CRC24 is
checked before that happens, so a truncated download can't be canonicalized into
a self-consistent file.

When a vendor rotates their key, apt starts failing - re-run `fetch-key` and
commit the diff.

## Tests

```sh
./test.py                  # tier 1: fast, no container runtime needed
./test.py --containers     # + tier 2: real package managers, per distro
./test.py --distro arch    # restrict tier 2 to one image
./test.py --rebuild        # force container images to rebuild
./test.py -k release       # only tests matching a substring
```

Stdlib `unittest`, no dependencies. Nothing touches the real machine: tier 1 runs
against a temp `HOME` and a temp copy of the repo, and tier 2 mounts the repo
**read-only** into throwaway containers.

**Tier 1** covers manifest and package-entry validation against this repo's real
data, the pure helpers (method resolution, URL templating, archive-traversal
rejection), and the whole CLI end to end. `release` downloads are served from a
local `http.server` fixture, so the suite is hermetic and works offline.

**Tier 2** runs sync.py inside Debian, Arch, Fedora, and Alpine containers, which
is the only way to exercise pacman and dnf from one machine - and Alpine proves
the tool degrades gracefully with no supported package manager rather than
crashing. It skips with a clear message when neither podman nor docker is usable.
Podman is preferred and works rootless.

Tier 1 points `PERSONAL_CONFIG_SYSROOT` at a temp directory, which relocates the
`/etc` paths a repository writes to. Setting it also drops `sudo` and suppresses
the metadata refresh, so the suite can exercise the privileged write path without
a password prompt, a network fetch, or any risk to the real machine.

Two known gaps: **brew/macOS can't be containerized**, so its command construction
is covered by unit tests only; and outside of adding a repository, `packages
install` still assumes the system package manager's metadata is current - sync.py
runs `apt-get update` only when a repository it manages actually changed.

## TODO

### Misc

- Misc scripts to run on sync. Should be idempotent and allow for that last layer of flexibility in the system
- Module destinations: `destination` may become an object keyed by OS
(`{"default": "~/.config/x", "darwin": "~/Library/x"}`) if a config ever needs
different paths per platform. Not implemented - today, OS differences are
handled inside the config files themselves (`uname` checks in zsh, `has('mac')`
in vim).

### Build source

**`build`** - clone a git repo and build it. **Designed, not yet implemented**:
`sync.py` recognizes the key and tells you to install manually.

```json
"neovim": {
	"apt": null,
	"build": {
		"git": "https://github.com/neovim/neovim",
		"ref": "v0.10.1",
		"deps": ["cmake", "gettext"],
		"build": ["make CMAKE_BUILD_TYPE=Release", "make install CMAKE_INSTALL_PREFIX=~/.local"]
	}
}
```

Clones to `~/.local/src/<name>` at the pinned `ref`, runs each `build` command
in the clone, and records a receipt like `release` does. `deps` are logical
package names resolved through this same file.

### Package archive files (.deb, .rpm, etc)

Should support pulling and running package archives.

### Appimage support

Should support Appimage files

### NPM support

Allow npm for a language specific package manager.
When doing this, start with adding https://github.com/oxidecomputer/skepsis.

### Third party repositories for the other managers

Only `apt` is implemented. dnf is the natural next one - a `.repo` INI file in
`/etc/yum.repos.d/`, which is the same "render bytes to a privileged path" shape,
so it reuses `write_privileged`, `repo_state` and `REFRESH_CMDS` and needs no
schema change. `brew tap` won't fit that mould, since a tap is a command rather
than a file. pacman needs an edit to `/etc/pacman.conf`, which has no drop-in
directory, so it's the messiest and least worth doing.

One known limitation: `uris` is literal, so a vendor that splits by distro
(`download.docker.com/linux/ubuntu` vs `.../debian`) is pinned to one of them.

