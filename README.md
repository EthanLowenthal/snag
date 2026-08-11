# snag

A terminal file browser for snagging files off servers. Two panes — local on the left,
remote on the right — and `c` to copy whatever is selected across, with live progress.
Every transfer is a real `rsync`, so resumes, permissions, and incremental syncs behave
exactly the way you already expect.

```
┌─ LOCAL  ~/Downloads ──────────────┬─ REMOTE · node3  /data/runs ──────┐
│ ..                                │ ..                                │
│  archive/                      —  │  2026-08-10/                   —  │
│  notes.md                   2.1K  │ ▌capture.raw                1.2G  │
│ ▌results.csv               48.0K  │  summary.json               812B  │
├───────────────────────────────────┴───────────────────────────────────┤
│ ← capture.raw  ███████████░░░░░  742.1M/1.2G  38.4MB/s  0:12          │
└───────────────────────────────────────────────────────────────────────┘
```

## Install

```sh
uv tool install git+https://github.com/EthanLowenthal/snag
```

Either builds snag into its own isolated environment and puts a single `snag` executable
on your `PATH` at `~/.local/bin` — nothing touches your system Python. If that directory
is not on your `PATH` yet, `uv tool update-shell` adds it.

```sh
snag                       # just run it
uv tool upgrade snag       # rebuild after pulling or editing
uv tool uninstall snag     # remove it and its environment
```

No `uv`? `pipx install .` gives the same isolated-environment result. Avoid plain
`pip install --user .` on macOS: Homebrew's Python is marked externally-managed and
will refuse the install outright.

### From a checkout, for hacking on it

```sh
uv venv .venv
uv pip install -e .
.venv/bin/snag             # picks up source edits with no reinstall
```

### Requirements

`rsync` must exist locally and on each server. macOS's built-in `openrsync` works; so
does GNU rsync, and snag adapts its progress parsing to whichever it finds. Python 3.11+.

## Servers

snag merges two sources, so hosts you already use just show up:

1. **`~/.ssh/config`** — every non-wildcard `Host` block, including `Include`d files.
   Aliases, keys, and jump hosts keep working because snag lets `ssh` resolve them.
2. **`~/.config/snag/servers.toml`** — extra hosts, or overrides that add a default
   remote path to a host ssh already knows.

```toml
[[server]]
name   = "build01"       # matches an ssh_config Host → ssh still resolves it
remote = "/data/runs"    # where the remote pane opens
local  = "~/Downloads"   # where the local pane opens

[[server]]
name = "nas"             # not in ssh_config → snag connects directly
host = "192.168.1.20"
user = "alice"
port = 2222
key  = "~/.ssh/id_ed25519"
```

Press `a` on the server list to add one through a form instead of editing the file.
The directories each pane was last in are remembered per server in
`~/.local/state/snag/state.json`, so reconnecting drops you back where you left off.

## Keys

| Key | Action |
| --- | --- |
| `enter` | Open directory (or `..` to go up) |
| `backspace` | Up one directory |
| `/` | Type a path (see below) |
| `~` | Type a path, starting at home |
| `tab` | Switch pane |
| `space` | Mark / unmark the entry under the cursor |
| `a` | Mark all / clear marks |
| `c` | Copy selection to the **other** pane |
| `x` | Cancel the running transfer |
| `X` | Clear finished transfers |
| `r` | Refresh the focused pane |
| `.` | Show/hide dotfiles |
| `esc` | Back to the server list |
| `q` | Quit |

With nothing marked, `c` copies whatever the cursor is on. Direction follows focus:
the focused pane is always the source, so `c` from the right pane pulls down, and `c`
from the left pane pushes up.

## Typing a path

`/` opens a path bar on the focused pane, holding the directory it is in, spelled out in
full and ready to be extended. `~` opens the same bar at your home directory. Matches
appear as you type and `tab` completes, the way a shell does.

| Key | In the path bar |
| --- | --- |
| `tab` | Complete as far as the names agree; with a match picked, step into it and keep going |
| `↑` `↓` | Choose between the matches at this level (also `ctrl+p` / `ctrl+n`) |
| `enter` | Take the pane there. On a directory the bar stays open, now holding that path |
| `esc` | Put the bar away |

So a deep directory is `/`, a few letters, `tab`, a few more letters, `tab`, `enter`.
`..` is not something to complete, so it folds into the path the moment you type it and
leaves you a level up, still typing — type it twice to go up twice. A typed `~` starts the
path over from home the same way, wherever in the path you type it.

`enter` goes wherever the bar names, whether or not you picked anything from the popup: a
plain directory opens, a half-typed name that can only mean one thing is finished for you,
and a *file* is a shortcut for "go to its directory and put the cursor on it" — what you
want after pasting a path out of a log. A name that matches several things, or nothing at
all, says so and leaves the pane where it is rather than taking it somewhere dead.

Dotfiles stay out of the matches until you type the leading `.`, `$VARS` expand on the
local side, and on the remote side `~` and every listing come from the same `rsync` a
real navigation uses — fetched in the background and remembered, so a tree you have
already walked completes instantly.

## Tests

```sh
uv pip install --group dev   # pytest and pytest-asyncio, once
.venv/bin/python -m pytest
```

No network needed: the "remote" side is a local directory reached through a patched
`_remote_spec`, so real `rsync` processes and the real Textual UI are driven end to end —
listings, marking, path-bar completion, a throttled transfer, cancel, and `--partial`
resume with a byte-for-byte comparison. Each test gets its own scratch tree, config and
state, so nothing one remembers leaks into the next.

## How it works

- **Listings** use `rsync --list-only` rather than `ssh ls`. The output is formatted by
  the *local* rsync, so parsing is identical whether the server is Linux, BSD, or macOS.
- **Sizing** runs `rsync --list-only -r` over the selection first, so a multi-file copy
  shows one honest percentage instead of restarting the bar per file.
- **Progress** is parsed from `--info=progress2` on GNU rsync ≥ 3.1, and from plain
  `--progress` on openrsync — where per-file byte counts are accumulated as each file
  is superseded, to reach the same cumulative number.
- **Transfers** run `rsync -a --partial --progress` on a worker thread, so the UI stays
  responsive and a cancelled copy can be resumed later.
- **Completion** reuses those listings. Every directory either pane has shown is cached,
  and anything the path bar needs beyond them is listed on a worker thread — so typing
  never blocks on the network, and the popup fills in when the answer arrives.
