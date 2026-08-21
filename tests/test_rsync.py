"""The transfer engine, driven with real rsync processes."""

from __future__ import annotations

import pytest

from snag import rsync as rsync_mod
from snag.config import Server
from snag.rsync import RsyncError, measure

from helpers import Throttled


def test_transfer_engine(tree):
    local, remote = tree(big=40_000_000)
    server = Server(name="fake")
    sources = [str(remote / "capture.raw"), str(remote / "logs"), str(remote / "summary.json")]

    expected = 40_000_000 + 13 + len("line\n") * 200
    total = measure(server, True, sources)
    assert total == expected, f"measure() summed {total:,}, selection holds {expected:,}"

    samples: list[tuple[int, str]] = []
    transfer = Throttled(server, from_remote=True, sources=sources, dest=str(local), total=total)
    transfer.run(lambda p: samples.append((p.done, p.eta)))

    dones = [d for d, _ in samples]
    assert all(a <= b for a, b in zip(dones, dones[1:])), f"progress went backwards: {dones}"
    assert dones[-1] == total, f"progress stopped at {dones[-1]:,}/{total:,}"
    assert len(samples) > 5, f"only {len(samples)} progress callbacks"

    etas = [e for _, e in samples if e]
    assert etas == sorted(etas, reverse=True), f"overall ETA does not decrease: {etas[:4]}…"

    on_disk = sum(f.stat().st_size for f in local.rglob("*") if f.is_file())
    assert on_disk == total, f"{on_disk:,} bytes landed of {total:,}"
    assert (local / "logs" / "run.log").exists(), "directory source was not recursed"


def test_unreachable_host_error(monkeypatch):
    """The message has to name the ssh failure, not rsync's generic wrapper."""
    monkeypatch.setattr(rsync_mod, "_remote_spec", lambda s, p: f"{s.target}:{p}")
    dead = Server(name="dead", host="127.0.0.1", port=1)

    with pytest.raises(RsyncError) as caught:
        rsync_mod.remote_list(dead, "/data")

    message = str(caught.value)
    assert "connect to host" in message, message
    assert "unexpected end of file" not in message, message


def _entry(name: str, size: int = 0, mtime: float = 0.0, is_dir: bool = False):
    return rsync_mod.Entry(name=name, is_dir=is_dir, size=size, mtime=mtime)


def test_sort_modes_order_entries():
    entries = [
        _entry("beta.txt", size=30, mtime=300),
        _entry("zulu", is_dir=True),
        _entry("alpha.zip", size=10, mtime=100),
        _entry("archive", is_dir=True),
        _entry("gamma.txt", size=20, mtime=200),
        _entry(".rc", size=5, mtime=50),
    ]

    def order(mode, reverse=False):
        return [e.name for e in rsync_mod.sort_entries(entries, mode, reverse)]

    assert order("name") == ["archive", "zulu", ".rc", "alpha.zip", "beta.txt", "gamma.txt"]
    # Biggest first, because that is what someone sorting by size came looking for.
    assert order("size") == ["archive", "zulu", "beta.txt", "gamma.txt", "alpha.zip", ".rc"]
    assert order("modified") == ["archive", "zulu", "beta.txt", "gamma.txt", "alpha.zip", ".rc"]
    # Extension, then name inside it; a leading dot is not an extension.
    assert order("kind") == ["archive", "zulu", ".rc", "beta.txt", "gamma.txt", "alpha.zip"]

    assert order("size", reverse=True) == [
        "zulu", "archive", ".rc", "alpha.zip", "gamma.txt", "beta.txt"
    ], "reverse did not flip both groups"
    for mode in rsync_mod.SORT_MODES:
        listed = rsync_mod.sort_entries(entries, mode, reverse=True)
        assert [e.name for e in listed[:2]] == ["zulu", "archive"], (
            f"{mode} reversed let a file above a directory"
        )
        assert sorted(e.name for e in listed) == sorted(e.name for e in entries), (
            f"{mode} lost or duplicated an entry"
        )


def test_sort_labels_show_the_real_direction():
    assert rsync_mod.sort_label("name", False) == "name ↑"
    assert rsync_mod.sort_label("name", True) == "name ↓"
    assert rsync_mod.sort_label("size", False) == "size ↓"
    assert rsync_mod.sort_label("modified", True) == "modified ↑"
