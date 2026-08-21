"""Everything that shells out: directory listings and transfers, both via rsync.

Listings go through `rsync --list-only` rather than `ssh ls` so the output format is
produced by the *local* rsync and is identical no matter what OS the server runs.
"""

from __future__ import annotations

import os
import re
import shlex
import subprocess
import threading
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache
from pathlib import Path

from .config import Server

RSYNC = "rsync"

# drwxr-xr-x          4,096 2026/08/11 11:58:48 name
_LIST_RE = re.compile(
    r"^([-dlbcpsD])\S{9}\s+([\d,]+)\s+(\d{4}/\d\d/\d\d\s+\d\d:\d\d:\d\d)\s+(.+)$"
)
#       1,234,567  45%   12.34MB/s    0:00:12
_PROGRESS_RE = re.compile(r"(\d[\d,]*)\s+(\d+)%\s+(\S+)\s+(\d+:\d\d:\d\d)")
_RATE_RE = re.compile(r"^([\d.]+)([KMGT]?)B/s$")

_SCALE = {"": 1, "K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}


def _rate_bytes(rate: str) -> float:
    m = _RATE_RE.match(rate)
    return float(m.group(1)) * _SCALE[m.group(2)] if m else 0.0


def _format_eta(seconds: float) -> str:
    seconds = int(min(seconds, 359999))
    return f"{seconds // 3600}:{seconds // 60 % 60:02d}:{seconds % 60:02d}"


class RsyncError(RuntimeError):
    """rsync exited non-zero; the message carries whatever it wrote to stderr."""


# openrsync writes "rsync(1234): error: ..."; GNU writes "rsync error: ...". Both bury
# the real cause (an ssh failure, a missing path) on an earlier line.
_NOISE = re.compile(r"^rsync[\s(:]|unexpected end of file|error in rsync protocol")


def _best_error(text: str, fallback: str) -> str:
    """Pick the most informative stderr line, skipping rsync's generic wrappers."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    for line in lines:
        if not _NOISE.search(line):
            return line
    return lines[0] if lines else fallback


@dataclass(frozen=True)
class Entry:
    name: str
    is_dir: bool
    size: int
    mtime: float | None
    is_link: bool = False

    @property
    def sort_key(self) -> tuple:
        return (not self.is_dir, self.name.lower())

    @property
    def kind(self) -> str:
        """The extension a listing sorts by; directories and dotfiles have none."""
        stem, dot, ext = self.name.rpartition(".")
        return ext.lower() if dot and stem and not self.is_dir else ""


# Cycle order for the sort toggle, and the label each mode shows in the pane status.
SORT_MODES = ("name", "size", "kind", "modified")

# Size and modified lead with the end people actually go looking for — the biggest
# file, the newest run -- so their natural order is descending; reversing flips them.
_SORT_KEYS = {
    "name": lambda e: (e.name.lower(),),
    "size": lambda e: (-e.size, e.name.lower()),
    "kind": lambda e: (e.kind, e.name.lower()),
    "modified": lambda e: (-(e.mtime or 0.0), e.name.lower()),
}


def sort_label(mode: str, reverse: bool) -> str:
    """`size ↓` — the mode plus the direction its primary key actually runs in."""
    descending = (mode in ("size", "modified")) != reverse
    return f"{mode} {'\u2193' if descending else '\u2191'}"


def sort_entries(entries: list[Entry], mode: str = "name", reverse: bool = False) -> list[Entry]:
    """Order a listing. Directories stay on top in every mode, reversed or not."""
    key = _SORT_KEYS.get(mode, _SORT_KEYS["name"])
    dirs = [e for e in entries if e.is_dir]
    files = [e for e in entries if not e.is_dir]
    return (sorted(dirs, key=key, reverse=reverse)
            + sorted(files, key=key, reverse=reverse))


@lru_cache(maxsize=1)
def supports_progress2() -> bool:
    """GNU rsync >= 3.1 has --info=progress2; macOS's openrsync does not."""
    try:
        done = subprocess.run(
            [RSYNC, "--info=help"], capture_output=True, timeout=10, text=True
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0 and "PROGRESS" in done.stdout.upper()


def _remote_spec(server: Server, path: str) -> str:
    """`host:'/quoted/path'`, where the path is expanded by the *remote* shell: quote it."""
    if path in ("", ".", "~"):
        return f"{server.target}:"
    return f"{server.target}:{shlex.quote(path)}"


def _run(cmd: list[str], timeout: int = 60) -> str:
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise RsyncError("timed out") from None
    except OSError as exc:
        raise RsyncError(str(exc)) from exc
    if done.returncode != 0:
        raise RsyncError(
            _best_error(done.stderr or done.stdout or "", f"rsync exited {done.returncode}")
        )
    return done.stdout


def _parse_listing(text: str) -> list[Entry]:
    entries = []
    for line in text.splitlines():
        m = _LIST_RE.match(line)
        if not m:
            continue
        kind, size, stamp, name = m.groups()
        if name in (".", ".."):
            continue
        is_link = kind == "l"
        if is_link:
            # "link -> target"; we only need the link's own name here.
            name = name.split(" -> ", 1)[0]
        try:
            mtime = datetime.strptime(stamp, "%Y/%m/%d %H:%M:%S").timestamp()
        except ValueError:
            mtime = None
        entries.append(
            Entry(name=name, is_dir=kind == "d", size=int(size.replace(",", "")),
                  mtime=mtime, is_link=is_link)
        )
    return entries


def remote_list(server: Server, path: str) -> list[Entry]:
    """List one remote directory (non-recursive)."""
    spec = _remote_spec(server, path.rstrip("/") + "/" if path not in ("", ".", "~") else path)
    out = _run([RSYNC, "--list-only", *server.rsync_shell, spec])
    return sorted(_parse_listing(out), key=lambda e: e.sort_key)


def local_list(path: Path) -> list[Entry]:
    entries = []
    try:
        with os.scandir(path) as it:
            for item in it:
                try:
                    stat = item.stat()
                    is_dir = item.is_dir()
                except OSError:
                    stat, is_dir = None, False
                entries.append(
                    Entry(
                        name=item.name,
                        is_dir=is_dir,
                        size=0 if is_dir or stat is None else stat.st_size,
                        mtime=stat.st_mtime if stat else None,
                        is_link=item.is_symlink(),
                    )
                )
    except OSError as exc:
        raise RsyncError(exc.strerror or str(exc)) from exc
    return sorted(entries, key=lambda e: e.sort_key)


def remote_home(server: Server) -> str:
    """Resolve the login directory, so the UI can show an absolute path from the start."""
    cmd = [*server.ssh_command, "-o", "BatchMode=yes", server.target, "pwd"]
    try:
        done = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        raise RsyncError(str(exc)) from exc
    if done.returncode != 0:
        detail = (done.stderr or "").strip().splitlines()
        raise RsyncError(detail[-1] if detail else "ssh failed")
    return done.stdout.strip() or "."


def measure(server: Server, from_remote: bool, paths: list[str]) -> int:
    """Total bytes behind a selection, so a multi-file transfer can show one real percentage."""
    total = 0
    for path in paths:
        spec = _remote_spec(server, path) if from_remote else path
        try:
            out = _run([RSYNC, "--list-only", "-r", *server.rsync_shell, spec], timeout=120)
        except RsyncError:
            continue
        total += sum(e.size for e in _parse_listing(out) if not e.is_dir)
    return total


# ------------------------------------------------------------------ transfer


@dataclass
class Progress:
    done: int
    total: int
    rate: str = ""
    eta: str = ""
    current: str = ""

    @property
    def fraction(self) -> float:
        return min(1.0, self.done / self.total) if self.total else 0.0


class Transfer:
    """A running rsync, parsed into cumulative byte counts.

    Handles both progress dialects: GNU's `--info=progress2` reports cumulative bytes
    directly, while openrsync's `--progress` reports per-file bytes that we accumulate
    ourselves as each file is superseded by the next.
    """

    def __init__(self, server: Server, from_remote: bool, sources: list[str], dest: str,
                 total: int = 0):
        self.server = server
        self.from_remote = from_remote
        self.sources = sources
        self.dest = dest
        self.total = total
        self._proc: subprocess.Popen | None = None
        self._lock = threading.Lock()
        self._cancelled = False

    @property
    def command(self) -> list[str]:
        cmd = [RSYNC, "-a", "--partial", "--progress", *self.server.rsync_shell]
        if supports_progress2():
            cmd += ["--info=progress2", "-v"]
        if self.from_remote:
            cmd += [_remote_spec(self.server, p) for p in self.sources]
            cmd += [self.dest.rstrip("/") + "/"]
        else:
            cmd += self.sources
            cmd += [_remote_spec(self.server, self.dest.rstrip("/") + "/")]
        return cmd

    def cancel(self) -> None:
        with self._lock:
            self._cancelled = True
            if self._proc and self._proc.poll() is None:
                self._proc.terminate()

    @property
    def cancelled(self) -> bool:
        return self._cancelled

    def run(self, on_progress) -> None:
        """Block until rsync finishes, calling `on_progress(Progress)` as it goes.

        Raises RsyncError on failure. Safe to call from a worker thread.
        """
        cumulative = supports_progress2()
        try:
            proc = subprocess.Popen(
                self.command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                bufsize=0,
            )
        except OSError as exc:
            raise RsyncError(str(exc)) from exc

        with self._lock:
            if self._cancelled:
                proc.terminate()
            self._proc = proc

        stderr_chunks: list[bytes] = []
        drain = threading.Thread(
            target=lambda: stderr_chunks.append(proc.stderr.read() or b""), daemon=True
        )
        drain.start()

        state = Progress(done=0, total=self.total)
        completed = 0  # bytes from files already finished (legacy mode only)
        current_bytes = 0
        buffer = b""

        assert proc.stdout is not None
        while True:
            chunk = proc.stdout.read(1)
            if not chunk:
                break
            if chunk not in (b"\r", b"\n"):
                buffer += chunk
                continue
            line, buffer = buffer.decode("utf-8", "replace").strip(), b""
            if not line:
                continue

            m = _PROGRESS_RE.search(line)
            if m:
                value = int(m.group(1).replace(",", ""))
                if cumulative:
                    state.done = value
                else:
                    current_bytes = value
                    state.done = completed + current_bytes
                state.rate = m.group(3)
                if cumulative:
                    state.eta = m.group(4)
                else:
                    # rsync's own ETA covers only the current file; derive one for the
                    # whole selection from the rate it just reported.
                    speed = _rate_bytes(state.rate)
                    remaining = max(0, state.total - state.done)
                    state.eta = _format_eta(remaining / speed) if speed and state.total else ""
            elif not line.startswith(("sending ", "receiving ", "sent ", "total size")):
                # A bare filename line: the previous file is done, bank its bytes.
                if not cumulative:
                    completed += current_bytes
                    current_bytes = 0
                    state.done = completed
                state.current = line.rstrip("/").split("/")[-1] or line
            else:
                continue
            on_progress(state)

        proc.wait()
        drain.join(timeout=5)
        if self._cancelled:
            return
        if proc.returncode != 0:
            raise RsyncError(
                _best_error(
                    b"".join(stderr_chunks).decode("utf-8", "replace"),
                    f"rsync exited {proc.returncode}",
                )
            )
        state.done = state.total or state.done
        on_progress(state)
