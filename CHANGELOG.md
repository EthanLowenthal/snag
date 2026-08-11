# Changelog

All notable changes to snag are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] — 2026-08-11

First stable release. Everything below is what snag does today.

### Browsing

- **Two panes, local and remote.** `tab` switches between them; the focused pane is
  always the source of a copy, so `c` from the right pulls down and `c` from the left
  pushes up.
- **Navigation** with `enter` to open a directory (or `..`), `backspace` to go up,
  `r` to refresh the focused pane, and `.` to show or hide dotfiles.
- **Remote listings come from `rsync --list-only`**, not `ssh ls`. The output is
  formatted by the *local* rsync, so parsing is identical whether the server runs
  Linux, BSD, or macOS.

### Path bar

- `/` opens a path bar on the focused pane, prefilled with the directory it is in;
  `~` opens the same bar at your home directory.
- `tab` completes as far as the candidate names agree, then steps into a picked match
  and keeps going. `↑`/`↓` (or `ctrl+p`/`ctrl+n`) choose between matches at the current
  level, `enter` takes the pane there, `esc` puts the bar away.
- `enter` needs nothing picked from the popup: it goes wherever the bar names, and
  finishes a half-typed name that can only have meant one thing. A name matching
  several things, or nothing at all, is reported and leaves the pane where it is.
- `enter` on a *file* jumps to its directory with the cursor on it — the useful
  behavior after pasting a path out of a log.
- `..` folds into the path as soon as it is typed, leaving you a level up and still
  typing. A typed `~` restarts the path from home the same way, wherever it lands.
- Dotfiles stay out of the matches until you type the leading `.`; `$VARS` expand on
  the local side.
- **Completions never block on the network.** Directories either pane has shown are
  cached, and anything else is listed on a worker thread — the popup fills in when
  the answer arrives.

### Transfers

- **Marking**: `space` marks or unmarks the entry under the cursor, `a` marks all or
  clears the marks. With nothing marked, `c` copies whatever the cursor is on.
- **Every transfer is a real `rsync -a --partial --progress`**, run on a worker thread
  so the UI stays responsive — resumes, permissions, and incremental syncs behave the
  way you already expect.
- **One honest percentage per copy**: the selection is sized with
  `rsync --list-only -r` up front, so a multi-file copy shows a single bar instead of
  restarting per file.
- **Progress on either rsync**: parsed from `--info=progress2` on GNU rsync ≥ 3.1, and
  from plain `--progress` on macOS's openrsync — where per-file byte counts are
  accumulated as each file is superseded to reach the same cumulative number.
- **Transfer panel** underneath the panes, listing each copy with its bar, current
  file, rate, and ETA. `x` cancels the running transfer (partial data is kept, so it
  can be resumed later), `X` clears finished rows.

### Servers

- **`~/.ssh/config` hosts show up automatically** — every non-wildcard `Host` block,
  including `Include`d files. Aliases, keys, and jump hosts keep working because snag
  lets `ssh` resolve them.
- **`~/.config/snag/servers.toml`** adds extra hosts, or overrides an ssh-known host
  with default local and remote paths. Fields: `name`, `host`, `user`, `port`, `key`,
  `local`, `remote`.
- **`a` on the server list** adds a server through a form instead of editing the file;
  `d` removes one, `r` reloads from disk.
- **Per-server directory memory**: the directories each pane was last in are saved to
  `~/.local/state/snag/state.json`, so reconnecting drops you back where you left off.

### Install and requirements

- Installs as a single `snag` executable via `uv tool install` or `pipx`.
- Python 3.11+, with `textual>=1.0` and `setproctitle>=1.3` the only runtime
  dependencies.
- **Shows up as `snag`** in `ps` and Activity Monitor, rather than as the `python` that
  happens to be running it.
- `rsync` must exist locally and on each server; macOS's built-in `openrsync` and GNU
  rsync both work, and snag adapts its progress parsing to whichever it finds.

[1.0.0]: https://github.com/EthanLowenthal/snag/releases/tag/v1.0.0
