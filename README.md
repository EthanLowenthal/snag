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
uv tool install .          # from a checkout
uv tool install git+https://github.com/YOURNAME/snag
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

## Tests

```sh
.venv/bin/python tests/smoke.py
```

No test framework and no network needed: the "remote" side is a local directory reached
through a patched `_remote_spec`, so real `rsync` processes and the real Textual UI are
driven end to end — listings, marking, a throttled transfer, cancel, and `--partial`
resume with a byte-for-byte comparison. Exits non-zero if any check fails.

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
