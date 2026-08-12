"""The command line: where to open, and what to start copying on arrival.

`snag` on its own is the server list, as always. Anything after it is read the way
`cp` and `rsync` read their arguments — sources first, destination last — with a
`server:` prefix naming one of the servers snag already knows.
"""

from __future__ import annotations

import os
import posixpath
from dataclasses import dataclass
from pathlib import Path

from .config import Server, load_servers

USAGE = """\
snag - a TUI file browser for snagging files off servers over rsync.

Usage:
  snag                          pick a server from the list
  snag SERVER                   open a server where you left off
  snag [SERVER:]PATH            open on a directory, or on a file's directory
                                with the cursor already on it
  snag SOURCE... DEST           open there and start the copy right away

A SERVER is any host snag lists: an entry in ~/.config/snag/servers.toml or a
Host in ~/.ssh/config. Any shorthand that can only mean one of them will do, so
`b01:` reaches `build01.example.com`. One side of a copy names a server, the
other does not.

  snag node3:/data/runs/capture.raw ~/Downloads
  snag ~/patch.diff node3:/srv/incoming
  snag node3:/data/runs

A trailing slash means "this directory"; without one, a path puts the cursor on
what it names. Local paths take $VARS and ~, and may be relative to where you
are standing.

Options:
  -h, --help                    show this and exit\
"""


class CliError(Exception):
    """A command line snag cannot act on. `main` prints it and exits non-zero."""


@dataclass(frozen=True)
class Start:
    """Where the command line points snag, and the copy it should start on arrival.

    A path of None leaves that side wherever the server was last browsing. Remote
    paths may still be relative or `~`-prefixed here: they are expanded against the
    login directory once the connection is up.
    """

    server: Server | None = None
    local: str | None = None
    local_keep: str | None = None  # entry to put the cursor on
    remote: str | None = None
    remote_keep: str | None = None
    probe: bool = False  # the remote path may name a file; find out before landing
    from_remote: bool = True
    sources: tuple[str, ...] = ()  # non-empty: copy these to the other pane on connect

    @property
    def copies(self) -> bool:
        return bool(self.sources)


def expand_remote(home: str, path: str) -> str:
    """Absolute form of a remote path, resolving `~` and relatives against `home`."""
    if path.startswith("/"):
        return posixpath.normpath(path)
    if path in ("", ".", "~"):
        return home
    return posixpath.normpath(posixpath.join(home, path.removeprefix("~/")))


# ------------------------------------------------------------------- parsing


@dataclass(frozen=True)
class _Arg:
    """One command-line path, and the server it belongs to if it named one."""

    server: Server | None
    path: str

    @property
    def is_remote(self) -> bool:
        return self.server is not None


def _known(servers: list[Server]) -> str:
    names = sorted({s.name for s in servers})
    return "known servers: " + (", ".join(names) if names else "none configured")


def _shorthand(name: str, servers: list[Server]) -> Server | None:
    """The one server a shorthand can only have meant, by prefix and then by substring.

    Hosts tend to be spelled out in full in ssh_config, so `b01:` should be allowed to
    mean `build01.example.com` while there is only one thing it could be.
    """
    if not name:
        return None
    for matches in (
        {s.name: s for s in servers if s.name.startswith(name)},
        {s.name: s for s in servers if name in s.name},
    ):
        if len(matches) == 1:
            return next(iter(matches.values()))
        if len(matches) > 1:
            raise CliError(f"{name!r} could mean any of: " + ", ".join(sorted(matches)))
    return None


def _server(name: str, arg: str, servers: list[Server]) -> Server | None:
    """Which server an argument names, if any. An exact name always wins."""
    exact = next((s for s in servers if s.name == name), None)
    if exact is not None:
        return exact
    if Path(os.path.expanduser(arg)).exists():
        return None  # a local path that merely happens to look like a server
    return _shorthand(name, servers)


def _split(arg: str, servers: list[Server]) -> _Arg:
    """Read `server:path`, falling back to a local path when no server is named."""
    name, sep, path = arg.partition(":")
    if not sep or not name or "/" in name:
        return _Arg(None, arg)
    server = _server(name, arg, servers)
    if server is not None:
        return _Arg(server, path)
    if Path(os.path.expanduser(arg)).exists():
        return _Arg(None, arg)
    raise CliError(f"no server named {name!r} ({_known(servers)})")


def _abs(path: str) -> str:
    return os.path.abspath(os.path.expanduser(os.path.expandvars(path)))


def _local_target(path: str) -> tuple[str, str | None]:
    """Where a local path opens a pane: itself if a directory, else its parent."""
    full = _abs(path)
    if path.endswith("/") or Path(full).is_dir():
        return full, None
    parent = str(Path(full).parent)
    if not Path(full).exists() and not Path(parent).is_dir():
        raise CliError(f"no such file or directory: {path}")
    return parent, Path(full).name


def _parent_of(arg: _Arg) -> str:
    """The directory holding a copy's source, so the pane opens looking at it."""
    if arg.is_remote:
        trimmed = arg.path.rstrip("/")
        return posixpath.dirname(trimmed) or ("/" if trimmed.startswith("/") else ".")
    return str(Path(_abs(arg.path)).parent)


def _leaf(arg: _Arg) -> str:
    trimmed = arg.path.rstrip("/")
    return posixpath.basename(trimmed) if arg.is_remote else Path(_abs(arg.path)).name


def _source_path(arg: _Arg) -> str:
    """A source as rsync should see it: normalised, and absolute on the local side."""
    if not arg.is_remote:
        full = _abs(arg.path)
        if not Path(full).exists():
            raise CliError(f"no such file or directory: {arg.path}")
        return full
    trimmed = arg.path.rstrip("/")
    if not trimmed:
        return "/" if arg.path.startswith("/") else "."  # `box:` is the login directory
    return posixpath.normpath(trimmed) if trimmed.startswith("/") else trimmed


def _browse(arg: _Arg) -> Start:
    if not arg.is_remote:
        directory, keep = _local_target(arg.path)
        return Start(local=directory, local_keep=keep)
    path = arg.path
    if path.rstrip("/") in ("", ".", "~"):
        return Start(server=arg.server, remote=path or None)
    if path.endswith("/"):
        return Start(server=arg.server, remote=path.rstrip("/") or "/")
    # Only the server can say whether this names a directory or a file, so ask on connect.
    return Start(server=arg.server, remote=path, probe=True)


def _copy(sources: list[_Arg], dest: _Arg, servers: list[Server]) -> Start:
    if len({s.is_remote for s in sources}) > 1:
        raise CliError("every source has to be on the same side of the copy")
    from_remote = sources[0].is_remote
    if from_remote and dest.is_remote:
        raise CliError(
            "snag copies between a server and this machine, so one side of a copy "
            "has to be a plain local path"
        )
    if not from_remote and not dest.is_remote:
        raise CliError(f"neither side of that copy names a server ({_known(servers)})")
    named = {a.server.name for a in [*sources, dest] if a.is_remote}
    if len(named) > 1:
        raise CliError("one server at a time: " + ", ".join(sorted(named)))
    server = next(a.server for a in [*sources, dest] if a.is_remote)

    paths = tuple(_source_path(s) for s in sources)
    # With one source the pane can point at it; with several, at wherever the first lives.
    keep = _leaf(sources[0]) if len(sources) == 1 else None
    source_dir = _parent_of(sources[0])

    if from_remote:
        return Start(
            server=server,
            local=_abs(dest.path),
            remote=source_dir,
            remote_keep=keep,
            from_remote=True,
            sources=paths,
        )
    remote_dest = dest.path.rstrip("/")
    return Start(
        server=server,
        local=source_dir,
        local_keep=keep,
        # A copy says where it is going, so a bare `box:` is the login directory rather
        # than wherever that server was last browsing.
        remote=posixpath.normpath(remote_dest) if remote_dest else "~",
        from_remote=False,
        sources=paths,
    )


def parse(argv: list[str], servers: list[Server] | None = None) -> Start | None:
    """Read the arguments after `snag`. None means "no arguments: the server list"."""
    if not argv:
        return None
    servers = load_servers() if servers is None else servers

    if len(argv) == 1 and "/" not in argv[0] and ":" not in argv[0]:
        # A lone word is a server to open where you left off, or a directory here.
        named = _server(argv[0], argv[0], servers)
        if named is not None:
            return Start(server=named)
        if not Path(os.path.expanduser(argv[0])).exists():
            raise CliError(f"no server or directory named {argv[0]!r} ({_known(servers)})")

    args = [_split(a, servers) for a in argv]
    if len(args) == 1:
        return _browse(args[0])
    return _copy(args[:-1], args[-1], servers)
